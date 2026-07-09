"""Command handlers: stats, export, search, edit, delete, help.

Grouped into a `CommandHandlers` class so dependencies (config, database)
are injected once in `main.py` rather than pulled from globals.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from config import CATEGORIES, CATEGORY_LABELS_EN, Config, category_display
from database import ExpenseDatabase

logger = logging.getLogger("expense_bot.commands")

# States for the /edit and /delete conversational flows.
EDIT_WAIT_REF, EDIT_WAIT_FIELD, EDIT_WAIT_VALUE = range(3)
DELETE_WAIT_REF, DELETE_WAIT_CONFIRM = range(3, 5)

EDITABLE_FIELDS = ["Amount", "Category", "Remark", "Bank", "Sender", "Receiver"]

# Matches free-text messages asking for an expense summary, so users can
# just type it directly to the bot instead of remembering /stats_month.
EXPENSE_QUERY_REGEX = re.compile(
    r"(สรุปค่าใช้จ่าย|ค่าใช้จ่ายเดือนนี้|ยอดใช้จ่าย|รายจ่ายเดือนนี้|ใช้เงินไปเท่าไหร่|"
    r"ใช้จ่ายไปเท่าไหร่|เดือนนี้ใช้ไป|expense\s*summary|spending\s*this\s*month|how\s*much.*spent)",
    re.IGNORECASE,
)


class CommandHandlers:
    def __init__(self, config: Config, db: ExpenseDatabase) -> None:
        self._config = config
        self._db = db

    def _authorized(self, user_id: int) -> bool:
        if not self._config.allowed_user_ids:
            return True
        return user_id in self._config.allowed_user_ids

    async def _guard(self, update: Update) -> bool:
        user = update.effective_user
        if user is None or not self._authorized(user.id):
            await update.effective_message.reply_text("คุณไม่มีสิทธิ์ใช้งานบอทนี้ครับ")
            return False
        return True

    # -- basic --------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "👋 ส่งรูปสลิปโอนเงิน (หรือไฟล์ PDF) มาให้ผมได้เลย แล้วผมจะบันทึกเป็นรายจ่ายให้อัตโนมัติ\n\n"
            "จ่ายเป็นเงินสดไม่มีสลิป? แค่พิมพ์จำนวนเงิน (เช่น \"150\") หรือใช้คำสั่ง /cash ก็ได้ครับ\n"
            "อยากรู้ว่าเดือนนี้ใช้จ่ายไปเท่าไหร่? ถามได้เลย เช่น \"สรุปค่าใช้จ่าย\"\n\n"
            "พิมพ์ /help เพื่อดูคำสั่งทั้งหมดที่ผมทำได้"
        )

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        text = (
            "*บอทบันทึกรายจ่าย*\n\n"
            "📷 ส่งรูปสลิปหรือไฟล์ PDF เพื่อบันทึกรายจ่าย\n"
            "💵 ไม่มีสลิป? พิมพ์จำนวนเงินตรงๆ (เช่น \"150\") หรือใช้คำสั่ง /cash `<จำนวนเงิน>` `[หมายเหตุ]` "
            "เพื่อบันทึกรายจ่ายเงินสดได้ทันที\n"
            "🗣 ถามได้เลย เช่น \"สรุปค่าใช้จ่าย\" / \"ค่าใช้จ่ายเดือนนี้เท่าไหร่\" "
            "แล้วผมจะสรุปยอดรวมเดือนนี้พร้อมแยกตามหมวดหมู่ให้\n\n"
            "*คำสั่ง*\n"
            "/cash `[จำนวนเงิน]` `[หมายเหตุ]` - บันทึกรายจ่ายเงินสด (ไม่มีสลิป)\n"
            "/stats\\_month - ยอดรวมเดือนนี้แยกตามหมวดหมู่ พร้อมเปอร์เซ็นต์\n"
            "/stats\\_year - ยอดรวมปีนี้แยกตามหมวดหมู่ พร้อมเปอร์เซ็นต์\n"
            "/export\\_csv - ส่งออกข้อมูลเป็นไฟล์ CSV\n"
            "/export\\_excel - ส่งออกข้อมูลเป็นไฟล์ Excel\n"
            "/search\\_category `<หมวดหมู่>` - ค้นหารายจ่ายตามหมวดหมู่\n"
            "/search\\_date `<YYYY-MM-DD> <YYYY-MM-DD>` - ค้นหารายจ่ายตามช่วงวันที่\n"
            "/edit - แก้ไขรายการที่บันทึกไว้ (ค้นหาด้วยเลขที่อ้างอิง)\n"
            "/delete - ลบรายการที่บันทึกไว้ (ค้นหาด้วยเลขที่อ้างอิง)\n"
            "/cancel - ยกเลิกการทำงานปัจจุบัน\n"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        await update.effective_message.reply_text("ยกเลิกแล้วครับ")
        return ConversationHandler.END

    # -- stats --------------------------------------------------------------

    async def stats_month(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        today = date.today()
        totals = self._db.monthly_stats(today.year, today.month, update.effective_user.id)
        await update.effective_message.reply_text(_format_totals(f"{today.strftime('%B %Y')}", totals))

    async def stats_year(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        today = date.today()
        totals = self._db.yearly_stats(today.year, update.effective_user.id)
        await update.effective_message.reply_text(_format_totals(str(today.year), totals))

    # -- export --------------------------------------------------------------

    async def export_csv(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        data = self._db.export_csv(update.effective_user.id)
        await update.effective_message.reply_document(
            document=io.BytesIO(data), filename="expenses.csv", caption="📄 ไฟล์ส่งออกรายจ่ายของคุณ (CSV)"
        )

    async def export_excel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        data = self._db.export_excel(update.effective_user.id)
        await update.effective_message.reply_document(
            document=io.BytesIO(data), filename="expenses.xlsx", caption="📊 ไฟล์ส่งออกรายจ่ายของคุณ (Excel)"
        )

    # -- search --------------------------------------------------------------

    async def search_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            valid = ", ".join(category_display(label) for _, label in CATEGORIES.values())
            await update.effective_message.reply_text(
                f"วิธีใช้: /search_category <หมวดหมู่>\nหมวดหมู่ที่ใช้ได้: {valid}"
            )
            return
        typed = " ".join(context.args)
        # The user sees (and is likely to type) the Thai display name;
        # normalize it back to the English canonical value the Sheet
        # actually stores before querying. Falls through unchanged if
        # already English or unrecognized.
        category = CATEGORY_LABELS_EN.get(typed, typed)
        results = self._db.search_by_category(category, update.effective_user.id)
        await update.effective_message.reply_text(
            _format_records(results, f"หมวดหมู่: {category_display(category)}")
        )

    async def search_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if len(context.args) != 2:
            await update.effective_message.reply_text(
                "วิธีใช้: /search_date <YYYY-MM-DD> <YYYY-MM-DD>"
            )
            return
        try:
            start = datetime.strptime(context.args[0], "%Y-%m-%d").date()
            end = datetime.strptime(context.args[1], "%Y-%m-%d").date()
        except ValueError:
            await update.effective_message.reply_text("รูปแบบวันที่ต้องเป็น YYYY-MM-DD")
            return
        results = self._db.search_by_date(start, end, update.effective_user.id)
        await update.effective_message.reply_text(
            _format_records(results, f"{start.isoformat()} ถึง {end.isoformat()}")
        )

    # -- edit (conversation) -----------------------------------------------

    async def edit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "กรุณาส่งเลขที่อ้างอิงของรายการที่ต้องการแก้ไข"
        )
        return EDIT_WAIT_REF

    async def edit_receive_ref(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ref = (update.effective_message.text or "").strip()
        row = self._find_row_by_reference(ref, update.effective_user.id)
        if row is None:
            await update.effective_message.reply_text("ไม่พบรายการที่มีเลขที่อ้างอิงนี้ครับ")
            return ConversationHandler.END
        context.user_data["edit_row"] = row
        await update.effective_message.reply_text(
            "ต้องการแก้ไขข้อมูลส่วนไหนครับ?\n" + ", ".join(EDITABLE_FIELDS)
        )
        return EDIT_WAIT_FIELD

    async def edit_receive_field(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        field_name = (update.effective_message.text or "").strip()
        matches = [f for f in EDITABLE_FIELDS if f.lower() == field_name.lower()]
        if not matches:
            await update.effective_message.reply_text(
                "ไม่ใช่ฟิลด์ที่แก้ไขได้ครับ กรุณาเลือกจาก: " + ", ".join(EDITABLE_FIELDS)
            )
            return EDIT_WAIT_FIELD
        context.user_data["edit_field"] = matches[0]
        await update.effective_message.reply_text(f"กรุณาใส่ค่าใหม่สำหรับ {matches[0]}:")
        return EDIT_WAIT_VALUE

    async def edit_receive_value(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        value = (update.effective_message.text or "").strip()
        field_name = context.user_data.get("edit_field")
        row = context.user_data.get("edit_row")
        if field_name == "Amount":
            try:
                Decimal(value)
            except Exception:  # noqa: BLE001
                await update.effective_message.reply_text("จำนวนเงินต้องเป็นตัวเลขครับ กรุณาลองใหม่")
                return EDIT_WAIT_VALUE
        if field_name == "Category":
            # Normalize a Thai-typed category back to the English canonical
            # value, same as /search_category - keeps the Sheet consistent.
            value = CATEGORY_LABELS_EN.get(value, value)
        self._db.edit_field(row, {field_name: value})
        await update.effective_message.reply_text(f"✅ อัปเดต {field_name} เรียบร้อยแล้ว")
        context.user_data.pop("edit_row", None)
        context.user_data.pop("edit_field", None)
        return ConversationHandler.END

    # -- delete (conversation) -----------------------------------------------

    async def delete_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "กรุณาส่งเลขที่อ้างอิงของรายการที่ต้องการลบ"
        )
        return DELETE_WAIT_REF

    async def delete_receive_ref(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ref = (update.effective_message.text or "").strip()
        row = self._find_row_by_reference(ref, update.effective_user.id)
        if row is None:
            await update.effective_message.reply_text("ไม่พบรายการที่มีเลขที่อ้างอิงนี้ครับ")
            return ConversationHandler.END
        context.user_data["delete_row"] = row
        await update.effective_message.reply_text(
            "ต้องการลบรายการนี้ใช่ไหมครับ? พิมพ์ 'ใช่' เพื่อยืนยัน หรือ 'ไม่' เพื่อยกเลิก"
        )
        return DELETE_WAIT_CONFIRM

    async def delete_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        answer = (update.effective_message.text or "").strip().lower()
        row = context.user_data.pop("delete_row", None)
        if answer not in ("yes", "y", "ใช่") or row is None:
            await update.effective_message.reply_text("ไม่ได้ลบรายการครับ")
            return ConversationHandler.END
        self._db.delete(row)
        await update.effective_message.reply_text("🗑 ลบรายการเรียบร้อยแล้ว")
        return ConversationHandler.END

    def _find_row_by_reference(self, reference_number: str, user_id: int) -> int | None:
        records = self._db.all_records(user_id)
        for idx, record in enumerate(records, start=2):  # header is row 1
            if record.get("Reference Number") == reference_number:
                return idx
        return None


def _format_totals(period_label: str, totals: dict[str, Decimal]) -> str:
    """Render a total + per-category breakdown with each category's share (%)."""
    if not totals:
        return f"ไม่มีรายการค่าใช้จ่ายในช่วง {period_label}"
    grand_total: Decimal = sum(totals.values())
    lines = [f"📊 สรุปค่าใช้จ่าย - {period_label}", ""]
    for category, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
        pct = (amount / grand_total * 100) if grand_total else Decimal("0")
        lines.append(f"  • {category_display(category)}: {amount:,.2f} บาท ({pct:.1f}%)")
    lines.append("")
    lines.append(f"รวมทั้งหมด: {grand_total:,.2f} บาท")
    return "\n".join(lines)


def _format_records(records: list[dict[str, str]], label: str, limit: int = 20) -> str:
    if not records:
        return f"ไม่พบรายการค่าใช้จ่ายสำหรับ {label}"
    lines = [f"🔍 พบ {len(records)} รายการสำหรับ {label}:"]
    for r in records[:limit]:
        lines.append(
            f"  {r.get('Date')} {r.get('Time')} - {r.get('Amount')} "
            f"({category_display(r.get('Category', ''))}) เลขอ้างอิง:{r.get('Reference Number') or '-'}"
        )
    if len(records) > limit:
        lines.append(f"  ...และอีก {len(records) - limit} รายการ")
    return "\n".join(lines)
