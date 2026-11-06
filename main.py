import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReplyParameters
from playwright.async_api import Browser, Page, async_playwright

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:  # нет tzdata -> UTC+2
    KYIV = timezone(timedelta(hours=2))

# ───────────────────────── Настройки ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "9200609418:AAEaQrYF_hNyarDPkS5h4u68q7XnCf72lSg")
URL = "https://www.dtek-krem.com.ua/ua/shutdowns"
CACHE_TTL = 300
PAGE_TIMEOUT = 30_000
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "600"))
SILENT_FROM, SILENT_TO = 22, 8
NOTIFY_WHEN_NO_OUTAGES = False
ADDRESS_FILE = Path(__file__).with_name("addresses.json")
SUBS_FILE = Path(__file__).with_name("subscriptions.json")
DEBUG_DIR = Path(__file__).with_name("debug")

# На странице две формы с одинаковыми id -> всё скоупим к #discon_form
FORM = "#discon_form"
FIELDS = {
    "city": (f"{FORM} #city", f"{FORM} #cityautocomplete-list"),
    "street": (f"{FORM} #street", f"{FORM} #streetautocomplete-list"),
    "house": (f"{FORM} #house_num", f"{FORM} #house_numautocomplete-list"),
}

# Скрывает всплывающее окно "Шановні клієнти!" и любые оверлеи, которые перекрывают поля
INIT_CSS_SCRIPT = """
document.addEventListener('DOMContentLoaded', () => {
  const s = document.createElement('style');
  s.textContent = '#modal-attention, .m-attention, .modal__overlay { display:none !important; pointer-events:none !important; }';
  document.head.appendChild(s);
});
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dtek")


# ───────────────────────── Модели и ошибки ─────────────────────────
class AddressError(Exception):
    """Пользователь ввёл несуществующий город / улицу / дом."""


class SiteError(Exception):
    """Сайт ДТЕК не ответил или изменилась вёрстка."""


@dataclass
class Address:
    city: str
    street: str
    house: str

    def key(self) -> str:
        return f"{self.city}|{self.street}|{self.house}".lower()


@dataclass
class ScheduleResult:
    group: str
    days: list[list[tuple[int, int]]]
    site_updated: str
    outage: str = ""      # текст о текущей аварии (если есть)

    def signature(self) -> str:
        return json.dumps([self.group, self.days], ensure_ascii=False)


# ───────────────────────── Парсинг адреса ─────────────────────────
def parse_address(raw: str) -> Address:
    """Формат: м. Боярка, вул. Переяславська, 10"""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 3:
        raise AddressError("Потрібно три частини через кому: місто, вулиця, будинок.")
    return Address(city=parts[0], street=parts[1], house=parts[2])


# ───────────────────────── Работа с графиком ─────────────────────────
FACT_MAP = {"yes": "on", "no": "off", "first": "first", "second": "second"}
CLASS_MAP = {
    "cell-non-scheduled": "on",
    "cell-scheduled": "off",
    "cell-first-half": "first",
    "cell-second-half": "second",
}


def cells_to_intervals(cells: list[str]) -> list[tuple[int, int]]:
    slots = [False] * 48
    for hour, c in enumerate(cells[:24]):
        if c == "off":
            slots[hour * 2] = slots[hour * 2 + 1] = True
        elif c == "first":
            slots[hour * 2] = True
        elif c == "second":
            slots[hour * 2 + 1] = True

    intervals: list[tuple[int, int]] = []
    start = None
    for i, off in enumerate(slots + [False]):
        if off and start is None:
            start = i
        elif not off and start is not None:
            intervals.append((start * 30, i * 30))
            start = None
    return intervals


def fmt_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_day(title: str, intervals: list[tuple[int, int]]) -> str:
    lines = [f"⚡️ Графік відключень на {title}:"]
    if not intervals:
        lines.append("🔋 00:00 — 24:00")
    else:
        lines += [f"🪫 {fmt_time(a)} — {fmt_time(b)}" for a, b in intervals]
    return "\n".join(lines)


def now_kyiv() -> datetime:
    return datetime.now(KYIV)


def build_message(res: ScheduleResult, changed: bool = False) -> str:
    now = now_kyiv().strftime("%d.%m.%Y %H:%M")
    parts = []
    if changed:
        parts.append("🔄 <b>Графік змінено</b>")
    parts.append(f"<b>{html.escape(res.group)}</b>")
    if res.outage:
        parts.append("🚨 " + html.escape(res.outage))
    for title, intervals in zip(["сьогодні", "завтра"], res.days):
        parts.append(format_day(title, intervals))
    if len(res.days) < 2:
        parts.append("ℹ️ Графік на завтра ще не опублікований.")
    parts.append(f"📢 {html.escape(res.site_updated or 'невідомо')}\n🤖 {now}")
    return "\n\n".join(parts)


def days_from_fact(fact: dict, group_key: str) -> list[list[str]]:
    data = fact.get("data") or {}
    today = int(fact.get("today") or 0)
    days: list[list[str]] = []
    for ts in (today, today + 86400):
        group_data = (data.get(str(ts)) or {}).get(group_key)
        if not group_data:
            continue
        days.append([FACT_MAP.get(group_data.get(str(h), "yes"), "on") for h in range(1, 25)])
    return days


# ───────────────────────── Скрапер ─────────────────────────
class DtekScraper:
    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._lock = asyncio.Semaphore(2)
        self._cache: dict[str, tuple[float, ScheduleResult]] = {}

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def get_schedule(self, addr: Address, use_cache: bool = True) -> ScheduleResult:
        if use_cache:
            cached = self._cache.get(addr.key())
            if cached and time.time() - cached[0] < CACHE_TTL:
                return cached[1]

        async with self._lock:
            ctx = await self._browser.new_context(locale="uk-UA", viewport={"width": 1366, "height": 900})
            await ctx.add_init_script(INIT_CSS_SCRIPT)
            page = await ctx.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)
            try:
                result = await self._scrape(page, addr)
            except (AddressError, SiteError):
                raise
            except Exception as e:
                await self._dump(page, "unexpected")
                raise SiteError(str(e)) from e
            finally:
                await ctx.close()

        self._cache[addr.key()] = (time.time(), result)
        return result

    async def _dump(self, page: Page, tag: str) -> None:
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%H%M%S")
            await page.screenshot(path=str(DEBUG_DIR / f"{tag}_{stamp}.png"), full_page=True)
            (DEBUG_DIR / f"{tag}_{stamp}.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass

    async def _dismiss_popups(self, page: Page) -> None:
        """Закрывает окно 'Шановні клієнти!' и любые оверлеи, перекрывающие форму."""
        try:
            close = page.locator("#modal-attention .modal__close")
            if await close.count() and await close.first.is_visible():
                await close.first.click(timeout=3000)
        except Exception:
            pass
        try:
            await page.evaluate(
                """() => {
                    try { if (window.MicroModal) MicroModal.close('modal-attention'); } catch (e) {}
                    document.querySelectorAll('.micromodal-slide.is-open, .modal__overlay').forEach(e => {
                        e.classList.remove('is-open');
                        e.style.display = 'none';
                        e.style.pointerEvents = 'none';
                    });
                    document.body.style.overflow = 'auto';
                }"""
            )
        except Exception:
            pass

    async def _choose(self, page: Page, field: str, wanted: str, what: str) -> None:
        input_sel, list_sel = FIELDS[field]
        await self._dismiss_popups(page)

        box = page.locator(input_sel)
        await box.wait_for(state="visible")
        await box.click()                      # click сам ждёт, пока поле станет enabled
        await box.fill("")
        await box.press_sequentially(wanted, delay=60)

        items = page.locator(f"{list_sel} > div")
        try:
            await items.first.wait_for(state="visible", timeout=8000)
        except Exception:
            raise AddressError(f"{what} «{wanted}» не знайдено на сайті ДТЕК.")

        values = await page.eval_on_selector_all(
            f"{list_sel} > div",
            "els => els.map(e => (e.querySelector('input')?.value ?? e.textContent).trim())",
        )

        def norm(s: str) -> str:
            return re.sub(r"\s+", " ", s.strip().lower().replace("’", "'"))

        def strip_prefix(s: str) -> str:
            return re.sub(r"^\S+\.\s*", "", s)

        w = norm(wanted)
        idx = None
        for i, v in enumerate(values):
            if norm(v) == w:
                idx = i
                break
        if idx is None:
            for i, v in enumerate(values):
                if norm(strip_prefix(v)) == norm(strip_prefix(wanted)):
                    idx = i
                    break
        if idx is None and field != "house":
            for i, v in enumerate(values):
                if w in norm(v):
                    idx = i
                    break
        if idx is None:
            raise AddressError(f"{what} «{wanted}» не знайдено. Перевірте написання.")

        await items.nth(idx).click()
        await page.wait_for_timeout(500)

    async def _scrape(self, page: Page, addr: Address) -> ScheduleResult:
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_selector(FIELDS["city"][0], state="visible")
        await page.wait_for_timeout(800)       # даём модалке показаться, чтобы сразу её убрать
        await self._dismiss_popups(page)

        await self._choose(page, "city", addr.city, "Населений пункт")
        await self._choose(page, "street", addr.street, "Вулицю")
        await self._choose(page, "house", addr.house, "Будинок")

        # группа появляется как data-key-group у #house_num (или текстом в #group-name)
        try:
            await page.wait_for_function(
                """() => {
                    const h = document.querySelector('#discon_form #house_num');
                    const k = h && h.getAttribute('data-key-group');
                    if (k && /GPV\\d+\\.\\d+/.test(k)) return true;
                    const s = document.querySelector('#discon_form #group-name span');
                    return !!(s && /\\d+\\.\\d+/.test(s.textContent));
                }""",
                timeout=15000,
            )
        except Exception:
            await self._dump(page, "no_group")
            raise SiteError("Група відключень не з'явилась після вибору адреси.")
        await page.wait_for_timeout(700)

        data = await page.evaluate(
            """() => {
                const h = document.querySelector('#discon_form #house_num');
                let key = h ? (h.getAttribute('data-key-group') || '') : '';
                const spanText = document.querySelector('#discon_form #group-name span')?.textContent.trim() || '';
                if (!/GPV\\d+\\.\\d+/.test(key)) {
                    const m = spanText.match(/(\\d+\\.\\d+)/);
                    key = m ? 'GPV' + m[1] : '';
                }
                const preset = (window.DisconSchedule && DisconSchedule.preset) || {};
                const names = preset.sch_names || {};
                const name = names[key] || (key ? 'Черга ' + key.replace('GPV', '') : spanText);
                const fact = (window.DisconSchedule && DisconSchedule.fact) ? DisconSchedule.fact : null;

                const domDays = [];
                document.querySelectorAll('#discon-fact table').forEach(t => {
                    t.querySelectorAll('tbody tr').forEach(tr => {
                        const cls = [...tr.querySelectorAll('td')]
                            .map(td => td.className).filter(c => c.includes('cell-'));
                        if (cls.length === 24) domDays.push(cls);
                    });
                });
                const domUpd = document.querySelector('#discon-fact .update')?.textContent.trim() || '';
                const hidden = document.querySelector('input[name="updateFact"]')?.value || '';

                const out = document.querySelector('#showCurOutage');
                let outage = '';
                if (out && out.offsetParent !== null) {
                    outage = (out.innerText || '').replace(/\\n{2,}/g, '\\n').trim();
                }
                return {key, name, fact, domDays, domUpd, hidden, outage};
            }"""
        )

        key = data["key"]
        days: list[list[str]] = []
        if data.get("fact") and key:
            days = days_from_fact(data["fact"], key)
        if not days and data["domDays"]:
            days = [
                [next((v for k, v in CLASS_MAP.items() if k in c), "on") for c in row]
                for row in data["domDays"][:2]
            ]
        if not days:
            await self._dump(page, "parse_fail")
            raise SiteError("Не вдалося розпізнати графік на сторінці.")

        site_updated = (
            (data.get("fact") or {}).get("update")
            or (data.get("fact") or {}).get("updateFact")
            or data["domUpd"]
            or data["hidden"]
            or ""
        )
        return ScheduleResult(
            group=data["name"],
            days=[cells_to_intervals(d) for d in days[:2]],
            site_updated=site_updated,
            outage=data.get("outage") or "",
        )


# ───────────────────────── Хранилище ─────────────────────────
def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_addresses() -> dict[str, str]:
    return _load(ADDRESS_FILE)


def save_address(user_id: int, raw: str) -> None:
    data = load_addresses()
    data[str(user_id)] = raw
    _save(ADDRESS_FILE, data)


# ───────────────────────── Мониторинг (подписки) ─────────────────────────
def is_silent() -> bool:
    h = now_kyiv().hour
    return h >= SILENT_FROM or h < SILENT_TO


async def process_subscription(bot: Bot, sub: dict, res: ScheduleResult) -> None:
    today = now_kyiv().date().isoformat()
    today_intervals = res.days[0] if res.days else []
    sig = res.signature()
    chat_id = sub["chat_id"]
    new_day = sub.get("date") != today or not sub.get("msg_id")

    if new_day:
        if not today_intervals and not NOTIFY_WHEN_NO_OUTAGES and not res.outage:
            return
        msg = await bot.send_message(chat_id, build_message(res), disable_notification=is_silent())
        sub.update(msg_id=msg.message_id, sig=sig, date=today)
        return

    if sig == sub.get("sig"):
        try:
            await bot.edit_message_text(build_message(res), chat_id=chat_id, message_id=sub["msg_id"])
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            log.warning("Edit failed (%s), sending a new message", e)
            msg = await bot.send_message(chat_id, build_message(res), disable_notification=is_silent())
            sub.update(msg_id=msg.message_id)
        return

    msg = await bot.send_message(
        chat_id,
        build_message(res, changed=True),
        reply_parameters=ReplyParameters(message_id=sub["msg_id"], allow_sending_without_reply=True),
        disable_notification=is_silent(),
    )
    sub.update(msg_id=msg.message_id, sig=sig)


async def poll_once(bot: Bot) -> None:
    subs = _load(SUBS_FILE)
    if not subs:
        return

    groups: dict[str, list[str]] = {}
    addrs: dict[str, Address] = {}
    for uid, sub in subs.items():
        try:
            addr = parse_address(sub["address"])
        except AddressError:
            continue
        groups.setdefault(addr.key(), []).append(uid)
        addrs[addr.key()] = addr

    for key, uids in groups.items():
        try:
            res = await scraper.get_schedule(addrs[key], use_cache=False)
        except AddressError as e:
            for uid in uids:
                try:
                    await bot.send_message(
                        subs[uid]["chat_id"],
                        f"❌ Підписку скасовано: {html.escape(str(e))}\nОформіть її знову через /subscribe.",
                    )
                except Exception:
                    pass
                subs.pop(uid, None)
            _save(SUBS_FILE, subs)
            continue
        except SiteError as e:
            log.error("Poll: site error for %s: %s", key, e)
            continue

        for uid in uids:
            try:
                await process_subscription(bot, subs[uid], res)
            except Exception:
                log.exception("Poll: failed for user %s", uid)
            _save(SUBS_FILE, subs)


async def poll_loop(bot: Bot) -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await poll_once(bot)
        except Exception:
            log.exception("poll_once crashed")
        await asyncio.sleep(POLL_INTERVAL)


# ───────────────────────── Бот ─────────────────────────
dp = Dispatcher()
scraper = DtekScraper()

HELP_TEXT = (
    "👋 <b>Привіт!</b> Я показую графік відключень світла ДТЕК (Київська область) "
    "на сьогодні та завтра.\n\n"
    "Напишіть адресу у форматі:\n"
    "<code>/dtek місто, вулиця, будинок</code>\n\n"
    "<b>Приклади:</b>\n"
    "<code>/dtek м. Боярка, вул. Білогородська, 10</code>\n"
    "<code>/dtek м. Бориспіль, вул. Київський Шлях, 45</code>\n"
    "<code>/dtek с. Гатне, вул. Лесі Українки, 3А</code>\n\n"
    "💾 Після першого успішного запиту адреса запам'ятовується — далі достатньо просто <code>/dtek</code>.\n\n"
    "🔔 <b>/subscribe</b> — автоматичні сповіщення: я стежитиму за графіком, оновлюватиму "
    "повідомлення, а при зміні графіка напишу нове у відповідь. З 22:00 до 08:00 — без звуку.\n"
    "🔕 <b>/unsubscribe</b> — вимкнути сповіщення."
)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@dp.message(Command("dtek"))
async def cmd_dtek(message: Message) -> None:
    args = (message.text or "").partition(" ")[2].strip()
    if not args:
        args = load_addresses().get(str(message.from_user.id), "")
        if not args:
            await message.answer("Вкажіть адресу:\n<code>/dtek м. Боярка, вул. Білогородська, 10</code>")
            return
    await handle_address(message, args)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message, bot: Bot) -> None:
    raw = (message.text or "").partition(" ")[2].strip() or load_addresses().get(str(message.from_user.id), "")
    if not raw:
        await message.answer("Вкажіть адресу:\n<code>/subscribe м. Боярка, вул. Білогородська, 10</code>")
        return
    try:
        addr = parse_address(raw)
    except AddressError as e:
        await message.answer(f"❌ {html.escape(str(e))}")
        return

    wait = await message.answer("⏳ Перевіряю адресу…")
    try:
        res = await scraper.get_schedule(addr, use_cache=False)
    except AddressError as e:
        await wait.edit_text(f"❌ {html.escape(str(e))}\nПеревірте, чи правильно введено адресу.")
        return
    except SiteError as e:
        log.error("Subscribe: site error: %s", e)
        await wait.edit_text("⚠️ Сайт ДТЕК зараз недоступний. Спробуйте пізніше.")
        return

    save_address(message.from_user.id, raw)
    subs = _load(SUBS_FILE)
    sub = {"chat_id": message.chat.id, "address": raw, "msg_id": None, "sig": None, "date": None}
    subs[str(message.from_user.id)] = sub
    await process_subscription(bot, sub, res)
    _save(SUBS_FILE, subs)

    note = "" if sub.get("msg_id") else "\nСьогодні відключень немає — напишу, щойно з'явиться графік."
    await wait.edit_text(f"🔔 Підписку оформлено: <b>{html.escape(res.group)}</b>.{note}")


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message) -> None:
    subs = _load(SUBS_FILE)
    if subs.pop(str(message.from_user.id), None) is not None:
        _save(SUBS_FILE, subs)
        await message.answer("🔕 Сповіщення вимкнено.")
    else:
        await message.answer("У вас немає активної підписки.")


@dp.message(F.text & ~F.text.startswith("/"))
async def plain_text(message: Message) -> None:
    if (message.text or "").count(",") == 2:
        await handle_address(message, message.text)
    else:
        await message.answer("Не зрозумів 🤔 Формат: <code>місто, вулиця, будинок</code>\nДив. /help")


async def handle_address(message: Message, raw: str) -> None:
    try:
        addr = parse_address(raw)
    except AddressError as e:
        await message.answer(f"❌ {html.escape(str(e))}\nПриклад: <code>/dtek м. Боярка, вул. Білогородська, 10</code>")
        return

    wait = await message.answer("⏳ Шукаю графік на сайті ДТЕК…")
    try:
        result = await scraper.get_schedule(addr)
    except AddressError as e:
        await wait.edit_text(f"❌ {html.escape(str(e))}\nПеревірте, чи правильно введено адресу.")
        return
    except SiteError as e:
        log.error("Site error: %s", e)
        await wait.edit_text("⚠️ Сайт ДТЕК зараз недоступний або змінив структуру. Спробуйте пізніше.")
        return

    save_address(message.from_user.id, raw)
    await wait.edit_text(build_message(result))


async def main() -> None:
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await scraper.start()
    poll_task = asyncio.create_task(poll_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        poll_task.cancel()
        await scraper.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())