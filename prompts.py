"""Italian system prompt and schema definitions for Gemini voice parsing."""

SYSTEM_PROMPT = """Sei l'assistente vocale di Spesabot, un bot Telegram per la gestione della lista della spesa.
Il tuo compito è ascoltare la registrazione vocale in italiano ed estrarre le azioni da compiere sulla lista della spesa.

Devi produrre esclusivamente un oggetto JSON conforme allo schema con la chiave "actions".

Azioni supportate:
1. "add": aggiunge un prodotto alla lista.
   - Usato quando l'utente vuole aggiungere, comprare, prendere o ricordare un prodotto (es. "aggiungi latte", "prendi 2 kg di mele", "ricordati il pane").
   - Campi: "type": "add", "item": "<nome prodotto>", "quantity": "<quantità o null>"

2. "remove": rimuove o segna come acquistato un prodotto dalla lista.
   - Usato quando l'utente ha comprato, preso o vuole eliminare un singolo prodotto (es. "ho preso le uova", "togli il burro", "togli 1 kg di mele").
   - Campi: "type": "remove", "item": "<nome prodotto>", "quantity": "<quantità o null>"

3. "clear": svuota completamente l'intera lista della spesa.
   - Richiede una chiara ed esplicita intenzione di cancellare o azzerare l'intera lista (es. "svuota la spesa", "cancella tutta la lista", "elimina tutto").
   - NON usare se l'utente intende solo togliere alcuni prodotti.
   - Campi: "type": "clear"

4. "bought_all_except": segna tutti i prodotti come acquistati tranne quelli specificati.
   - Usato quando l'utente dice di aver comprato tutto tranne determinati articoli (es. "ho preso tutto tranne il latte e i biscotti", "ho comprato tutto eccetto 2 litri di latte").
   - Campi: "type": "bought_all_except", "items": [{"item": "<nome>", "quantity": "<quantità o null>"}, ...]

Regole di normalizzazione ed estrazione:
- Nome prodotto ("item"):
  * Usa il nome comune in italiano al singolare minuscolo (es. "mele" -> "mela", "pomodori" -> "pomodoro", "zucchine" -> "zucchina").
  * Mantieni il plurale solo per i sostantivi che in italiano si usano tipicamente al plurale (es. "biscotti", "cereali", "spinaci", "uova").
  * Rimuovi articoli determinativi, indeterminativi o partitivi (es. "il latte" -> "latte", "dei pomodori" -> "pomodoro", "un pane" -> "pane").
- Quantità ("quantity"):
  * Se specificata, estrai una stringa concisa con numero e unità di misura (es. "2 kg", "500 g", "1 l", "6", "1 confezione").
  * Se non specificata, imposta quantity a null.
- Ordine delle azioni:
  * Mantieni nell'array "actions" lo stesso ordine cronologico in cui le azioni sono pronunciate nella registrazione.
- Audio non pertinente, rumore o silenzio:
  * Se l'audio contiene solo rumore, silenzio, colpi di tosse, o conversazione non inerente alla lista della spesa (es. "ciao come stai"), restituisci una lista di azioni vuota: {"actions": []}.
  * Non inventare o dedurre prodotti non esplicitamente menzionati.
"""

ACTIONS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "actions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "type": {
                        "type": "STRING",
                        "enum": ["add", "remove", "clear", "bought_all_except"],
                    },
                    "item": {"type": "STRING", "nullable": True},
                    "quantity": {"type": "STRING", "nullable": True},
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "item": {"type": "STRING"},
                                "quantity": {"type": "STRING", "nullable": True},
                            },
                            "required": ["item"],
                        },
                        "nullable": True,
                    },
                },
                "required": ["type"],
            },
        }
    },
    "required": ["actions"],
}


def sanitize_actions(payload: object) -> dict:
    """Normalize parsed JSON envelope so it strictly conforms to actions.validate_actions."""
    if not isinstance(payload, dict) or "actions" not in payload:
        return {"actions": []}
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list):
        return {"actions": []}

    cleaned = []
    for action in raw_actions:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        if kind == "clear":
            cleaned.append({"type": "clear"})
        elif kind in ("add", "remove"):
            item_name = action.get("item")
            if isinstance(item_name, str) and item_name.strip():
                item_obj: dict = {"type": kind, "item": item_name.strip()}
                qty = action.get("quantity")
                if isinstance(qty, str) and qty.strip():
                    item_obj["quantity"] = qty.strip()
                cleaned.append(item_obj)
        elif kind == "bought_all_except":
            raw_items = action.get("items")
            if isinstance(raw_items, list):
                valid_items = []
                for it in raw_items:
                    if isinstance(it, dict):
                        sub_name = it.get("item")
                        if isinstance(sub_name, str) and sub_name.strip():
                            sub_obj: dict = {"item": sub_name.strip()}
                            sub_qty = it.get("quantity")
                            if isinstance(sub_qty, str) and sub_qty.strip():
                                sub_obj["quantity"] = sub_qty.strip()
                            valid_items.append(sub_obj)
                cleaned.append({"type": "bought_all_except", "items": valid_items})
    return {"actions": cleaned}
