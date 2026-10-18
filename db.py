"""SQLite persistence for Spesabot. No Telegram or Gemini dependencies."""

from dataclasses import dataclass
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
from typing import Literal


@dataclass(frozen=True)
class Item:
    id: int
    chat_id: int
    name: str
    quantity: str | None
    collected: bool


@dataclass(frozen=True)
class PendingAdd:
    id: int
    chat_id: int
    name: str
    quantity: str | None


@dataclass(frozen=True)
class PendingRemoval:
    id: int
    chat_id: int
    name: str
    quantity: str


def numeric_quantity(value: str | None) -> Decimal | None:
    """Accept finite nonnegative numbers, including Italian decimal commas."""
    if value is None:
        return None
    try:
        number = Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _name(value: str) -> str:
    name = " ".join(value.split()).casefold()
    if not name:
        raise ValueError("Item name must not be empty")
    return name


def _quantity(value: str | None) -> str | None:
    return value.strip() or None if value is not None else None


class Store:
    """One connection, owned by one thread; writes are transactional.

    Names use normalized whitespace and case, without fuzzy matching.
    Pending decisions are scoped to their originating chat and resolved FIFO.
    """

    def __init__(self, path: str | Path = "spesabot.sqlite3") -> None:
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._savepoint = 0
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    quantity TEXT,
                    collected INTEGER NOT NULL DEFAULT 0 CHECK(collected IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(chat_id, name)
                );
                CREATE TABLE IF NOT EXISTS pending_adds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    quantity TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS pending_chat
                    ON pending_adds(chat_id, id);
                CREATE TABLE IF NOT EXISTS pending_removals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    original_quantity TEXT,
                    original_collected INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pending_removal_chat
                    ON pending_removals(chat_id, id);
                CREATE TABLE IF NOT EXISTS processed_messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    processed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (chat_id, message_id)
                );
            """)

    @contextmanager
    def transaction(self):
        """Compose storage operations into one atomic batch, with nested rollback."""
        nested = self._db.in_transaction
        self._savepoint += 1
        savepoint = f"operation_{self._savepoint}"
        self._db.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if nested:
                self._db.execute(f"ROLLBACK TO {savepoint}")
                self._db.execute(f"RELEASE {savepoint}")
            else:
                self._db.rollback()
            raise
        else:
            if nested:
                self._db.execute(f"RELEASE {savepoint}")
            else:
                self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def _item(row: sqlite3.Row) -> Item:
        return Item(row["id"], row["chat_id"], row["name"],
                    row["quantity"], bool(row["collected"]))

    def list_items(self, chat_id: int, *, include_collected: bool = False) -> list[Item]:
        rows = self._db.execute(
            "SELECT * FROM items WHERE chat_id = ? AND (? OR collected = 0) ORDER BY id",
            (chat_id, include_collected),
        )
        return [self._item(row) for row in rows]

    def get_item(self, chat_id: int, name: str) -> Item | None:
        row = self._db.execute(
            "SELECT * FROM items WHERE chat_id = ? AND name = ?", (chat_id, _name(name))
        ).fetchone()
        return self._item(row) if row else None

    def pending_adds(self, chat_id: int) -> list[PendingAdd]:
        return [PendingAdd(row["id"], row["chat_id"], row["name"], row["quantity"])
                for row in self._db.execute(
                    "SELECT * FROM pending_adds WHERE chat_id = ? ORDER BY id", (chat_id,)
                )]

    def add_item(self, chat_id: int, name: str, quantity: str | None = None) -> Item | PendingAdd:
        name, quantity = _name(name), _quantity(quantity)
        with self.transaction():
            if self.get_item(chat_id, name) is not None:
                cursor = self._db.execute(
                    "INSERT INTO pending_adds(chat_id, name, quantity) VALUES (?, ?, ?)",
                    (chat_id, name, quantity),
                )
                assert cursor.lastrowid is not None
                return PendingAdd(cursor.lastrowid, chat_id, name, quantity)
            cursor = self._db.execute(
                "INSERT INTO items(chat_id, name, quantity) VALUES (?, ?, ?)",
                (chat_id, name, quantity),
            )
            assert cursor.lastrowid is not None
            return Item(cursor.lastrowid, chat_id, name, quantity, False)

    def resolve_add(self, chat_id: int, pending_id: int,
                    choice: Literal["sum", "overwrite", "cancel"]) -> bool:
        """Return False for expired or foreign callbacks; reject out-of-order ones.

        If the original item has since been deleted, accepting restores it using
        the pending quantity. A repeated callback cannot apply a decision twice.
        """
        if choice not in {"sum", "overwrite", "cancel"}:
            raise ValueError("Unknown duplicate decision")
        with self.transaction():
            pending = self.pending_adds(chat_id)
            target = next((p for p in pending if p.id == pending_id), None)
            if target is None:
                return False
            if target != pending[0]:
                raise ValueError("Resolve the oldest pending addition first")
            if choice != "cancel":
                existing = self.get_item(chat_id, target.name)
                quantity = target.quantity
                if choice == "sum" and existing is not None:
                    old, new = numeric_quantity(existing.quantity), numeric_quantity(quantity)
                    if old is None or new is None:
                        raise ValueError("Both quantities must be numeric to sum")
                    quantity = format(old + new, "f")
                self._db.execute(
                    """INSERT INTO items(chat_id, name, quantity) VALUES (?, ?, ?)
                       ON CONFLICT(chat_id, name) DO UPDATE
                       SET quantity = excluded.quantity, collected = 0""",
                    (chat_id, target.name, quantity),
                )
            self._db.execute("DELETE FROM pending_adds WHERE id = ? AND chat_id = ?",
                             (pending_id, chat_id))
            return True

    def update_item(self, chat_id: int, name: str, *, quantity: str | None,
                    collected: bool = False) -> bool:
        with self.transaction():
            return self._db.execute(
                "UPDATE items SET quantity = ?, collected = ? WHERE chat_id = ? AND name = ?",
                (_quantity(quantity), int(collected), chat_id, _name(name)),
            ).rowcount > 0

    def delete_item(self, chat_id: int, name: str) -> bool:
        with self.transaction():
            return self._db.execute(
                "DELETE FROM items WHERE chat_id = ? AND name = ?", (chat_id, _name(name))
            ).rowcount > 0

    def clear(self, chat_id: int) -> None:
        """Clear items and expire pending additions so old buttons cannot restore them."""
        with self.transaction():
            self._db.execute("DELETE FROM items WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM pending_adds WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM pending_removals WHERE chat_id = ?", (chat_id,))

    def remove_item(self, chat_id: int, name: str,
                    quantity: str | None = None) -> PendingRemoval | None:
        """Subtract plain numbers; ask for overwrite/keep when subtraction is ambiguous."""
        quantity = _quantity(quantity)
        with self.transaction():
            item = self.get_item(chat_id, name)
            if item is None or item.collected:
                return None
            if quantity is None:
                self.delete_item(chat_id, name)
                return None
            old, amount = numeric_quantity(item.quantity), numeric_quantity(quantity)
            if old is not None and amount is not None:
                if amount >= old:
                    self.delete_item(chat_id, name)
                else:
                    self.update_item(chat_id, name, quantity=format(old - amount, "f"))
                return None
            cursor = self._db.execute(
                """INSERT INTO pending_removals
                   (chat_id, name, quantity, item_id, original_quantity, original_collected)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chat_id, item.name, quantity, item.id, item.quantity, int(item.collected)),
            )
            assert cursor.lastrowid is not None
            return PendingRemoval(cursor.lastrowid, chat_id, item.name, quantity)

    def pending_removals(self, chat_id: int) -> list[PendingRemoval]:
        return [PendingRemoval(row["id"], row["chat_id"], row["name"], row["quantity"])
                for row in self._db.execute(
                    "SELECT * FROM pending_removals WHERE chat_id = ? ORDER BY id", (chat_id,)
                )]

    def resolve_removal(self, chat_id: int, pending_id: int,
                        choice: Literal["overwrite", "keep"]) -> bool:
        """Overwrite means use the requested quantity as the remaining target.

        Expired, foreign, or changed-item decisions return False. Stale decisions
        are discarded without modifying the item or recreating a deleted item.
        """
        if choice not in {"overwrite", "keep"}:
            raise ValueError("Unknown removal decision")
        with self.transaction():
            row = self._db.execute(
                "SELECT * FROM pending_removals WHERE chat_id = ? AND id = ?",
                (chat_id, pending_id),
            ).fetchone()
            if row is None:
                return False
            if self.pending_removals(chat_id)[0].id != pending_id:
                raise ValueError("Resolve the oldest pending removal first")
            item = self.get_item(chat_id, row["name"])
            valid = (item is not None and item.id == row["item_id"]
                     and item.quantity == row["original_quantity"]
                     and item.collected == bool(row["original_collected"]))
            if valid and choice == "overwrite":
                self.update_item(chat_id, row["name"], quantity=row["quantity"])
            self._db.execute("DELETE FROM pending_removals WHERE id = ?", (pending_id,))
            return valid

    def bought_all_except(self, chat_id: int,
                          exceptions: list[tuple[str, str | None]]) -> None:
        with self.transaction():
            self._db.execute("UPDATE items SET collected = 1 WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM pending_removals WHERE chat_id = ?", (chat_id,))
            for name, quantity in exceptions:
                name, quantity = _name(name), _quantity(quantity)
                self._db.execute(
                    """INSERT INTO items(chat_id, name, quantity) VALUES (?, ?, ?)
                       ON CONFLICT(chat_id, name) DO UPDATE SET collected = 0,
                       quantity = COALESCE(excluded.quantity, items.quantity)""",
                    (chat_id, name, quantity),
                )

    def record_processed_message(self, chat_id: int, message_id: int) -> bool:
        """Record a processed message ID. Returns True if recorded, False if already processed."""
        cursor = self._db.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO processed_messages (chat_id, message_id) VALUES (?, ?)",
            (chat_id, message_id),
        )
        return cursor.rowcount > 0

