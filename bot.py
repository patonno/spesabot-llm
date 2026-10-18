"""Italian Telegram commands and confirmation flows. Run with python bot.py."""

import logging
import re
import secrets
from typing import Literal

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from actions import apply_actions
from config import Settings
from db import Store, numeric_quantity
from gemini import (
    GeminiClient,
    GeminiError,
    GeminiParseError,
    GeminiRateLimitError,
    GeminiServiceError,
)


HELP = ("Invia un messaggio vocale, lo spesabot organizzerà la lista della spesa in condivisione con il tuo magnifico partner.\n\n"
        "/lista — mostra la lista della spesa\n/svuota — svuota la lista della spesa\n"
        "/aiuto — mostra questo aiuto\n\nInvia un messaggio vocale per aggiornare la spesa.")

MAX_VOICE_DURATION = 120  # 2 minutes
MAX_VOICE_SIZE = 5 * 1024 * 1024  # 5 MB


def main_menu():
    return [Button("📋 LISTA", callback_data="menu:list"),
            Button("❓ AIUTO", callback_data="menu:help"),
            Button("🗑️ SVUOTA", callback_data="menu:clear")]


def display_item(name, quantity):
    return f"{quantity} × {name}" if quantity is not None else name


class GroceryBot:
    
    def __init__(self, store: Store, settings: Settings, gemini_client: GeminiClient | None = None):
        self.store = store
        self.settings = settings
        self.clear_tokens: dict[int, str] = {}
        if gemini_client is not None:
            self.gemini_client = gemini_client
        elif settings.gemini_api_key:
            self.gemini_client = GeminiClient(settings.gemini_api_key, settings.gemini_model)
        else:
            self.gemini_client = None

    async def close(self) -> None:
        if self.gemini_client is not None:
            await self.gemini_client.aclose()


    async def authorized(self, update):
        allowed = (update.effective_chat is not None
                   and update.effective_chat.id in self.settings.allowed_chat_ids)
        if update.callback_query:
            try:
                await update.callback_query.answer(
                    text=None if allowed else "Chat non autorizzata.", show_alert=not allowed)
            except TelegramError:
                # An expired query must not prevent a fresh list response.
                pass
        return allowed

    def pending_prompt(self):
        additions = self.store.pending_adds()
        if additions:
            pending = additions[0]
            item = self.store.get_item(pending.name)
            current = display_item(item.name, item.quantity) if item else "non più in lista"
            requested = display_item(pending.name, pending.quantity)
            text = f'Hai provato ad aggiungere {requested}.\n\nAbbiamo già in lista {current}.\n\nScegli come procedere.'
            buttons = []
            if (item and numeric_quantity(item.quantity) is not None
                    and numeric_quantity(pending.quantity) is not None):
                buttons.append(Button("➕ SOMMA", callback_data=f"dup:sum:{pending.id}"))
            buttons.extend([Button("🔄 SOVRASCRIVI", callback_data=f"dup:ovw:{pending.id}"),
                            Button("❌ ANNULLA", callback_data=f"dup:cancel:{pending.id}")])
            return text, [buttons]
        removals = self.store.pending_removals()
        if removals:
            pending = removals[0]
            text = (f'Non posso sottrarre con certezza la quantità di {pending.name}.\n\n'
                    f'Vuoi impostare la quantità ancora da comprare a {pending.quantity}, '
                    'oppure mantenere la lista attuale?')
            return text, [[Button("🔄 SOVRASCRIVI", callback_data=f"rem:ovw:{pending.id}"),
                           Button("↩️ MANTIENI", callback_data=f"rem:keep:{pending.id}")]]
        return "", []

    async def show(self, update, context, notice="", *, confirmation=False, include_list=False):
        chat_id = update.effective_chat.id
        listing = ""
        if include_list:
            items = self.store.list_items()
            listing = "📋 LISTA DELLA SPESA\n\n" + (
                "\n".join(f"• {display_item(i.name, i.quantity)}" for i in items)
                if items else "La lista è vuota.")
        if confirmation:
            token = secrets.token_hex(8)
            self.clear_tokens[chat_id] = token
            prompt = "Sei sicuro di voler svuotare la lista?"
            rows = [[Button("✅ Sì, svuota", callback_data=f"clear:confirm:{token}"),
                     Button("❌ Annulla", callback_data=f"clear:cancel:{token}")]]
        else:
            prompt, rows = self.pending_prompt()
        text = "\n\n".join(part for part in (notice, listing, prompt) if part)
        if not text:
            text = "Nessuna operazione da mostrare."
        # 2000 codepoints fit within Telegram's 4096 UTF-16-unit text limit.
        for offset in range(0, len(text), 2000):
            last = offset + 2000 >= len(text)
            await context.bot.send_message(
                chat_id=chat_id, text=text[offset:offset + 2000], parse_mode=None,
                reply_markup=InlineKeyboardMarkup((rows if last else []) + [main_menu()]))

    async def start(self, update, context):
        if await self.authorized(update):
            await self.show(update, context, "Benvenuto in Spesabot!\n")

    async def lista(self, update, context):
        if await self.authorized(update):
            await self.show(update, context, include_list=True)

    async def help(self, update, context):
        if await self.authorized(update):
            await self.show(update, context, HELP)

    async def clear(self, update, context):
        if await self.authorized(update):
            await self.show(update, context, confirmation=True)

    async def unsupported(self, update, context):
        if await self.authorized(update):
            await self.show(update, context, "Usa /lista, /svuota, /aiuto o invia una nota vocale.")

    async def handle_voice(self, update, context):
        if not await self.authorized(update):
            return
        chat_id = update.effective_chat.id
        message = update.effective_message
        voice = getattr(message, "voice", None)
        if voice is None:
            return

        if voice.duration and voice.duration > MAX_VOICE_DURATION:
            await self.show(update, context, "Audio troppo lungo. Limite: 2 minuti.")
            return

        if voice.file_size and voice.file_size > MAX_VOICE_SIZE:
            await self.show(update, context, "Audio troppo pesante. Limite: 5 MB.")
            return

        if self.gemini_client is None:
            await self.show(update, context, "Integrazione vocale non configurata.")
            return

        status_msg = None
        try:
            status_msg = await context.bot.send_message(chat_id=chat_id, text="Escuchando...")
        except Exception:
            pass

        async def _cleanup_status():
            nonlocal status_msg
            if status_msg is not None:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                status_msg = None

        try:
            voice_file = await context.bot.get_file(voice.file_id)
            audio_bytes = await voice_file.download_as_bytearray()
        except Exception as exc:
            await _cleanup_status()
            logging.getLogger(__name__).error("Failed to download voice note: %s", type(exc).__name__)
            await self.show(update, context, "Impossibile scaricare l'audio. Riprova.")
            return

        if len(audio_bytes) > MAX_VOICE_SIZE:
            await _cleanup_status()
            await self.show(update, context, "Audio troppo pesante. Limite: 5 MB.")
            return

        try:
            parsed = await self.gemini_client.parse_voice_note(
                bytes(audio_bytes), mime_type=getattr(voice, "mime_type", None) or "audio/ogg"
            )
        except GeminiRateLimitError:
            await _cleanup_status()
            await self.show(update, context, "Limite richieste raggiunto. Riprova tra poco.")
            return
        except GeminiServiceError:
            await _cleanup_status()
            await self.show(update, context, "Servizio temporaneamente non disponibile. Riprova tra qualche istante.")
            return
        except (GeminiParseError, GeminiError, Exception) as exc:
            await _cleanup_status()
            logging.getLogger(__name__).error("Voice parsing failed: %s", type(exc).__name__)
            await self.show(update, context, "Impossibile elaborare il messaggio vocale. Riprova.")
            return

        message_id = getattr(message, "message_id", 0)
        with self.store.transaction():
            if not self.store.record_processed_message(chat_id, message_id):
                await _cleanup_status()
                return
            actions = parsed.get("actions", [])
            if actions:
                apply_actions(self.store, parsed)

        await _cleanup_status()
        if not actions:
            await self.show(update, context, "Non ho riconosciuto prodotti o azioni per la lista della spesa.")
        else:
            await self.show(update, context, "Lista aggiornata.")

    async def process_actions(self, update, context, payload):
        """Entry point for testing action dispatch; never accepts Telegram text as JSON."""
        if await self.authorized(update):
            apply_actions(self.store, payload)
            await self.show(update, context, "Lista aggiornata.")


    async def callback(self, update, context):
        if not await self.authorized(update):
            return
        chat_id = update.effective_chat.id
        data = update.callback_query.data
        if not isinstance(data, str):
            data = ""
        if data == "menu:list":
            await self.show(update, context, include_list=True)
            return
        if data == "menu:help":
            await self.show(update, context, HELP)
            return
        if data == "menu:clear":
            await self.show(update, context, confirmation=True)
            return
        notice = "Pulsante scaduto o non valido."
        clear = re.fullmatch(r"clear:(confirm|cancel):([0-9a-f]{16})", data or "")
        decision = re.fullmatch(r"(dup|rem):(sum|ovw|cancel|keep):([1-9][0-9]{0,18})", data or "")
        if clear and self.clear_tokens.get(chat_id) == clear[2]:
            del self.clear_tokens[chat_id]
            if clear[1] == "confirm":
                self.store.clear()
                notice = "Lista svuotata."
            else:
                notice = "Operazione annullata."
        elif decision:
            kind, choice, pending_id_str = decision.groups()
            pending_id = int(pending_id_str)
            if pending_id <= 9223372036854775807:
                try:
                    if kind == "dup" and choice in ("sum", "ovw", "cancel"):
                        dup_choices: dict[str, Literal["sum", "overwrite", "cancel"]] = {
                            "sum": "sum", "ovw": "overwrite", "cancel": "cancel"
                        }
                        if self.store.resolve_add(pending_id, dup_choices[choice]):
                            notice = "Scelta registrata."
                    elif kind == "rem" and choice in ("ovw", "keep"):
                        rem_choices: dict[str, Literal["overwrite", "keep"]] = {
                            "ovw": "overwrite", "keep": "keep"
                        }
                        if self.store.resolve_removal(pending_id, rem_choices[choice]):
                            notice = "Scelta registrata."
                except ValueError:
                    notice = "La scelta non è disponibile. Controlla la richiesta attuale."
        await self.show(update, context, notice)

    async def error(self, update, context):
        # Exception text and HTTP URLs may contain tokens or private message data.
        logging.getLogger(__name__).error("Update failed: %s", type(context.error).__name__)
        if isinstance(update, Update) and await self.authorized(update):
            try:
                await self.show(update, context, "Si è verificato un errore. Controlla la lista prima di riprovare.")
            except Exception:
                logging.getLogger(__name__).error("Unable to send error response")


def build_application(settings: Settings, store: Store, gemini_client: GeminiClient | None = None) -> Application:
    controller = GroceryBot(store, settings, gemini_client=gemini_client)
    builder = Application.builder().token(settings.telegram_token).concurrent_updates(False)
    builder.post_shutdown(lambda app: app.bot_data["controller"].close())
    application = builder.build()
    application.bot_data["controller"] = controller
    for command, handler in (("start", controller.start), ("lista", controller.lista),
                             ("aiuto", controller.help), ("svuota", controller.clear)):
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(CallbackQueryHandler(controller.callback))
    application.add_handler(MessageHandler(filters.VOICE, controller.handle_voice))
    application.add_handler(MessageHandler(filters.ALL, controller.unsupported))
    application.add_error_handler(controller.error)
    return application



def main():
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.load()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    with Store(settings.database_path) as store:
        application = build_application(settings, store)
        application.run_polling(allowed_updates=["message", "callback_query"],
                                drop_pending_updates=False)


if __name__ == "__main__":
    main()
