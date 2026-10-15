import asyncio
import csv
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BASE = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(BASE / "data" / "bot.db"))
SCORES_LOG = Path(os.environ.get("SCORES_LOG", str(BASE / "data" / "scores_log.csv")))
STATIONS_FILE = BASE / "stations.json"
TOKEN = os.environ.get("BOT_TOKEN", "")
CHIEF_IDS = {int(x) for x in os.environ.get("CHIEF_IDS", "").replace(" ", "").split(",") if x}
# 1 = команда может брать одну и ту же станцию повторно, 0 = нельзя
ALLOW_REVISIT = os.environ.get("ALLOW_REVISIT", "1") == "1"
GROUP_RE = re.compile(r"^1\d\d$")

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS teams(
            tg_id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            registered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stations(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            organizer_id INTEGER,
            team_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS visits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            station_id INTEGER NOT NULL,
            taken_at TEXT NOT NULL,
            taken_by TEXT NOT NULL,
            points INTEGER,
            scored_at TEXT,
            freed_at TEXT
        );
        """
    )
    # станции из stations.json: обновляем название/место/организатора, занятость не трогаем
    for s in json.loads(STATIONS_FILE.read_text(encoding="utf-8")):
        db.execute(
            """INSERT INTO stations(id, name, location, organizer_id) VALUES(?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
               location=excluded.location, organizer_id=excluded.organizer_id""",
            (s["id"], s["name"], str(s["location"]), s.get("organizer_id")),
        )
    db.commit()
    if not SCORES_LOG.exists():
        with SCORES_LOG.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["time", "team", "station", "points", "organizer_id"])


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_score(team: str, station: int, points: int, organizer_id: int):
    """Каждое начисление дописывается в CSV (append-only), чтобы баллы не потерялись."""
    with SCORES_LOG.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([now(), team, station, points, organizer_id])
        f.flush()
        os.fsync(f.fileno())


# ---------- роли ----------
def is_chief(uid: int) -> bool:
    return uid in CHIEF_IDS


def org_stations(uid: int):
    return db.execute("SELECT * FROM stations WHERE organizer_id=? ORDER BY id", (uid,)).fetchall()


def is_organizer(uid: int) -> bool:
    return is_chief(uid) or bool(org_stations(uid))


def get_team(uid: int):
    return db.execute("SELECT * FROM teams WHERE tg_id=?", (uid,)).fetchone()


def team_total(tg_id: int) -> int:
    r = db.execute("SELECT COALESCE(SUM(points),0) s FROM visits WHERE team_id=?", (tg_id,)).fetchone()
    return r["s"]


def resolve_station(uid: int, arg: str | None):
    """Станция организатора: из привязки, либо по номеру (главному можно любую)."""
    if arg and arg.isdigit():
        st = db.execute("SELECT * FROM stations WHERE id=?", (int(arg),)).fetchone()
        if st and (is_chief(uid) or st["organizer_id"] == uid):
            return st, None
        return None, "Такой станции нет или она не привязана к вам."
    mine = org_stations(uid)
    if len(mine) == 1:
        return mine[0], None
    if not mine:
        return None, "Укажите номер станции."
    return None, "У вас несколько станций, укажите номер."


dp = Dispatcher()

# ---------- /start ----------
CAPTAIN_HELP = (
    "Команды капитана:\n"
    "/register <номер группы> — зарегистрировать команду (например, /register 101)\n"
    "/list — свободные станции"
)
ORG_HELP = (
    "Команды организатора:\n"
    "/ball <баллы> — начислить баллы (0–10) команде на вашей станции\n"
    "/free <номер> — освободить станцию\n"
    "/busy <номер> <группа> — вручную занять станцию за команду"
)
CHIEF_HELP = (
    "Команды главного организатора:\n"
    "/all_stations — статус всех станций\n"
    "/leaderboard — таблица лидеров\n"
    "/team <название> — информация о команде\n"
    "/export — выгрузить баллы в CSV\n"
    "(команды организатора тоже доступны; если станций несколько — "
    "добавляйте номер: /ball 8 16)"
)


@dp.message(Command("start"))
async def cmd_start(m: Message):
    uid = m.from_user.id
    parts = []
    if is_organizer(uid):
        mine = org_stations(uid)
        text = "Привет, организатор! 👋\n"
        if mine:
            text += "Ваши станции: " + ", ".join(f"{s['id']} ({s['location']})" for s in mine) + "\n"
        parts.append(text + "\n" + ORG_HELP)
        if is_chief(uid):
            parts.append(CHIEF_HELP)
    else:
        team = get_team(uid)
        head = "Привет, капитан! 👋\n"
        if team:
            head += f"Ваша команда: {team['name']}\n"
        parts.append(head + "\n" + CAPTAIN_HELP)
    await m.answer("\n\n".join(parts))


# ---------- капитан ----------
@dp.message(Command("register"))
async def cmd_register(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not GROUP_RE.match(arg):
        await m.answer("Укажите номер группы в формате 1**, например: /register 101")
        return
    uid = m.from_user.id
    if get_team(uid):
        await m.answer(f"Вы уже зарегистрированы как команда {get_team(uid)['name']}.")
        return
    try:
        db.execute("INSERT INTO teams(tg_id, name, registered_at) VALUES(?,?,?)", (uid, arg, now()))
        db.commit()
    except sqlite3.IntegrityError:
        await m.answer("Команда с таким номером уже зарегистрирована. Если это ошибка — напишите организатору.")
        return
    await m.answer(f"✅ Команда {arg} зарегистрирована.\nСвободные станции: /list")


def free_stations_for(team_id: int):
    rows = db.execute("SELECT * FROM stations WHERE team_id IS NULL ORDER BY id").fetchall()
    if ALLOW_REVISIT:
        return rows
    done = {
        r["station_id"]
        for r in db.execute("SELECT station_id FROM visits WHERE team_id=?", (team_id,))
    }
    return [s for s in rows if s["id"] not in done]


@dp.message(Command("list"))
async def cmd_list(m: Message):
    team = get_team(m.from_user.id)
    if not team:
        await m.answer("Сначала зарегистрируйте команду: /register <номер группы>")
        return
    cur = db.execute("SELECT id FROM stations WHERE team_id=?", (team["tg_id"],)).fetchone()
    if cur:
        await m.answer(f"Вы сейчас на станции {cur['id']}. Дождитесь, пока организатор её освободит.")
        return
    free = free_stations_for(team["tg_id"])
    if not free:
        hidden = db.execute("SELECT COUNT(*) c FROM stations WHERE team_id IS NULL").fetchone()["c"]
        if hidden:
            await m.answer("Свободные станции есть, но вы уже побывали на каждой из них. Повторно заходить нельзя.")
        else:
            await m.answer("Свободных станций сейчас нет. Попробуйте чуть позже: /list")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{s['name']} · {s['location']}", callback_data=f"take:{s['id']}")]
            for s in free
        ]
    )
    await m.answer("Свободные станции (нажмите, чтобы занять):", reply_markup=kb)


@dp.callback_query(F.data.startswith("take:"))
async def cb_take(c: CallbackQuery):
    team = get_team(c.from_user.id)
    if not team:
        await c.answer("Сначала /register", show_alert=True)
        return
    sid = int(c.data.split(":")[1])
    if db.execute("SELECT 1 FROM stations WHERE team_id=?", (team["tg_id"],)).fetchone():
        await c.answer("Вы уже заняли другую станцию.", show_alert=True)
        return
    if not ALLOW_REVISIT and db.execute(
        "SELECT 1 FROM visits WHERE team_id=? AND station_id=?", (team["tg_id"], sid)
    ).fetchone():
        await c.answer("Вы уже были на этой станции.", show_alert=True)
        return
    # атомарно: займёт только один, если нажали одновременно
    cur = db.execute("UPDATE stations SET team_id=? WHERE id=? AND team_id IS NULL", (team["tg_id"], sid))
    if cur.rowcount == 0:
        db.commit()
        await c.answer("Эту станцию только что заняли 😔", show_alert=True)
        await c.message.edit_text("Список устарел, обновите: /list")
        return
    db.execute(
        "INSERT INTO visits(team_id, station_id, taken_at, taken_by) VALUES(?,?,?,?)",
        (team["tg_id"], sid, now(), "captain"),
    )
    db.commit()
    st = db.execute("SELECT * FROM stations WHERE id=?", (sid,)).fetchone()
    await c.message.edit_text(
        f"✅ Вы успешно взяли станцию {st['id']}.\n\n"
        f"📌 Название: {st['name']}\n"
        f"📍 Местоположение: {st['location']}"
    )
    await c.answer()
    if st["organizer_id"]:
        try:
            await c.bot.send_message(
                st["organizer_id"],
                f"🔔 Команда {team['name']} заняла вашу станцию {st['id']} ({st['location']}).",
            )
        except Exception as e:  # организатор мог не нажать /start у бота
            logging.warning("не удалось уведомить организатора %s: %s", st["organizer_id"], e)


# ---------- организатор ----------
async def require_org(m: Message) -> bool:
    if not is_organizer(m.from_user.id):
        await m.answer("Эта команда доступна только организаторам.")
        return False
    return True


def active_visit(station_id: int):
    return db.execute(
        "SELECT v.*, t.name tname FROM visits v JOIN teams t ON t.tg_id=v.team_id "
        "WHERE v.station_id=? AND v.freed_at IS NULL ORDER BY v.id DESC LIMIT 1",
        (station_id,),
    ).fetchone()


@dp.message(Command("ball"))
async def cmd_ball(m: Message, command: CommandObject):
    if not await require_org(m):
        return
    args = (command.args or "").split()
    if not args or not args[0].isdigit() or not 0 <= int(args[0]) <= 10:
        await m.answer("Использование: /ball <баллы от 0 до 10>")
        return
    pts = int(args[0])
    st, err = resolve_station(m.from_user.id, args[1] if len(args) > 1 else None)
    if err:
        await m.answer(err)
        return
    v = active_visit(st["id"])
    if not v or st["team_id"] is None:
        await m.answer(f"Станцию {st['id']} сейчас никто не занимает.")
        return
    db.execute("UPDATE visits SET points=?, scored_at=? WHERE id=?", (pts, now(), v["id"]))
    db.commit()
    log_score(v["tname"], st["id"], pts, m.from_user.id)
    await m.answer(
        f"✅ Команде {v['tname']} на станции {st['id']} начислено баллов: {pts}.\n"
        f"Когда команда закончит — /free {st['id']}"
    )


@dp.message(Command("free"))
async def cmd_free(m: Message, command: CommandObject):
    if not await require_org(m):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer("Использование: /free <номер станции>")
        return
    st, err = resolve_station(m.from_user.id, arg)
    if err:
        await m.answer(err)
        return
    v = active_visit(st["id"])
    if v:
        db.execute("UPDATE visits SET freed_at=? WHERE id=?", (now(), v["id"]))
    db.execute("UPDATE stations SET team_id=NULL WHERE id=?", (st["id"],))
    db.commit()
    who = f" Команда {v['tname']} откреплена." if v else ""
    note = ""
    if v and v["points"] is None:
        note = "\n⚠️ Баллы этой команде не были выставлены."
    await m.answer(f"🟢 Станция {st['id']} свободна.{who}{note}")


@dp.message(Command("busy"))
async def cmd_busy(m: Message, command: CommandObject):
    if not await require_org(m):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit() or not GROUP_RE.match(args[1]):
        await m.answer("Использование: /busy <номер станции> <номер группы>, например /busy 16 101")
        return
    st, err = resolve_station(m.from_user.id, args[0])
    if err:
        await m.answer(err)
        return
    team = db.execute("SELECT * FROM teams WHERE name=?", (args[1],)).fetchone()
    if not team:
        await m.answer(f"Команда {args[1]} не зарегистрирована.")
        return
    if st["team_id"] is not None:
        await m.answer(f"Станция {st['id']} уже занята. Сначала /free {st['id']}.")
        return
    if db.execute("SELECT 1 FROM stations WHERE team_id=?", (team["tg_id"],)).fetchone():
        await m.answer(f"Команда {team['name']} уже занимает другую станцию.")
        return
    db.execute("UPDATE stations SET team_id=? WHERE id=?", (team["tg_id"], st["id"]))
    db.execute(
        "INSERT INTO visits(team_id, station_id, taken_at, taken_by) VALUES(?,?,?,?)",
        (team["tg_id"], st["id"], now(), f"organizer:{m.from_user.id}"),
    )
    db.commit()
    await m.answer(f"🔴 Станция {st['id']} занята командой {team['name']}.")
    try:
        await m.bot.send_message(
            team["tg_id"],
            f"Организатор закрепил за вами станцию {st['id']}.\n\n"
            f"📌 Название: {st['name']}\n📍 Местоположение: {st['location']}",
        )
    except Exception:
        pass


# ---------- главный организатор ----------
async def require_chief(m: Message) -> bool:
    if not is_chief(m.from_user.id):
        await m.answer("Эта команда доступна только главному организатору.")
        return False
    return True


@dp.message(Command("all_stations"))
async def cmd_all(m: Message):
    if not await require_chief(m):
        return
    lines = ["Статус станций:"]
    for s in db.execute("SELECT s.*, t.name tname FROM stations s LEFT JOIN teams t ON t.tg_id=s.team_id ORDER BY s.id"):
        if s["team_id"] is None:
            lines.append(f"🟢 {s['id']} ({s['location']}) — свободна")
        else:
            lines.append(f"🔴 {s['id']} ({s['location']}) — занята: команда {s['tname']}")
    await m.answer("\n".join(lines))


@dp.message(Command("leaderboard"))
async def cmd_leaderboard(m: Message):
    if not await require_chief(m):
        return
    rows = db.execute(
        "SELECT t.name, COALESCE(SUM(v.points),0) total, COUNT(v.points) scored "
        "FROM teams t LEFT JOIN visits v ON v.team_id=t.tg_id "
        "GROUP BY t.tg_id ORDER BY total DESC, t.name"
    ).fetchall()
    if not rows:
        await m.answer("Команд пока нет.")
        return
    lines = ["🏆 Таблица лидеров:"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. Команда {r['name']} — {r['total']} (станций оценено: {r['scored']})")
    await m.answer("\n".join(lines))


@dp.message(Command("team"))
async def cmd_team(m: Message, command: CommandObject):
    if not await require_chief(m):
        return
    name = (command.args or "").strip()
    if not name:
        await m.answer("Использование: /team <название>, например /team 101")
        return
    t = db.execute("SELECT * FROM teams WHERE name=?", (name,)).fetchone()
    if not t:
        await m.answer(f"Команда {name} не найдена.")
        return
    cur = db.execute("SELECT id FROM stations WHERE team_id=?", (t["tg_id"],)).fetchone()
    lines = [f"Команда {t['name']}", f"Баллы: {team_total(t['tg_id'])}"]
    lines.append(f"Сейчас на станции: {cur['id']}" if cur else "Сейчас ни на одной станции")
    vs = db.execute("SELECT * FROM visits WHERE team_id=? ORDER BY id", (t["tg_id"],)).fetchall()
    if vs:
        lines.append("Станции:")
        for v in vs:
            p = "—" if v["points"] is None else v["points"]
            lines.append(f"• станция {v['station_id']}: {p}")
    await m.answer("\n".join(lines))


@dp.message(Command("export"))
async def cmd_export(m: Message):
    if not await require_chief(m):
        return
    path = Path(DB_PATH).parent / "export_visits.csv"
    rows = db.execute(
        "SELECT t.name team, v.station_id, v.points, v.taken_at, v.scored_at, v.freed_at, v.taken_by "
        "FROM visits v JOIN teams t ON t.tg_id=v.team_id ORDER BY v.id"
    ).fetchall()
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["team", "station", "points", "taken_at", "scored_at", "freed_at", "taken_by"])
        for r in rows:
            w.writerow(list(r))
    await m.answer_document(FSInputFile(path), caption="Все посещения и баллы")
    await m.answer_document(FSInputFile(SCORES_LOG), caption="Журнал начислений (каждое /ball)")


async def main():
    if not TOKEN:
        raise SystemExit("Задайте BOT_TOKEN")
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
