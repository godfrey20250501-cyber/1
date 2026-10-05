import os
import random
import ast
import operator
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from flask import Flask
from threading import Thread
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TOKEN_BOT_DISCORD_TOKEN", "").strip()
DB_FILE = os.getenv("TOKEN_BOT_DB_FILE", "token_bot.db")
GUILD_ID = int(os.getenv("TOKEN_BOT_GUILD_ID", "0") or 0)
ORGANIZER_ROLE_ID = int(os.getenv("TOKEN_BOT_ORGANIZER_ROLE_ID", "0") or 0)
ENTRY_ROLE_ID = int(os.getenv("TOKEN_BOT_ENTRY_ROLE_ID", "0") or 0)

if not TOKEN:
    raise SystemExit("TOKEN_BOT_DISCORD_TOKEN 未設定")

DB_LOCK = threading.Lock()
app = Flask(__name__)

@app.get("/")
def health():
    return "Token bot is running", 200

def keep_alive():
    port = int(os.getenv("PORT", "8080"))
    Thread(target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False), daemon=True).start()

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def init_db():
    with DB_LOCK, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS balances (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS rates (
            guild_id INTEGER NOT NULL, currency TEXT NOT NULL,
            tokens_per_unit INTEGER NOT NULL,
            PRIMARY KEY (guild_id, currency)
        );
        CREATE TABLE IF NOT EXISTS exchanges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, currency TEXT NOT NULL,
            token_amount INTEGER NOT NULL, game_amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
            completed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS lotteries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
            organizer_id INTEGER NOT NULL, pool INTEGER NOT NULL,
            winners INTEGER NOT NULL, description TEXT, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', channel_id INTEGER, message_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS lottery_entries (
            lottery_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, added_by INTEGER NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY (lottery_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            entry_role_id INTEGER NOT NULL DEFAULT 0,
            updated_by INTEGER,
            updated_at TEXT NOT NULL
        );
        """)
        columns = {row["name"] for row in c.execute("PRAGMA table_info(lotteries)").fetchall()}
        if "status" not in columns: c.execute("ALTER TABLE lotteries ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
        if "channel_id" not in columns: c.execute("ALTER TABLE lotteries ADD COLUMN channel_id INTEGER")
        if "message_id" not in columns: c.execute("ALTER TABLE lotteries ADD COLUMN message_id INTEGER")

def is_admin(interaction):
    return bool(getattr(interaction.user, "guild_permissions", None) and interaction.user.guild_permissions.administrator)

def is_organizer(interaction):
    if is_admin(interaction):
        return True
    return bool(ORGANIZER_ROLE_ID and any(r.id == ORGANIZER_ROLE_ID for r in getattr(interaction.user, "roles", [])))

def get_entry_role_id(guild_id):
    with DB_LOCK, db() as c:
        row = c.execute("SELECT entry_role_id FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
    return row["entry_role_id"] if row else ENTRY_ROLE_ID

def can_enter_lottery(guild_id, member):
    """每個伺服器可獨立設定；0 代表不限制參加身分組。"""
    role_id = get_entry_role_id(guild_id)
    return not role_id or any(role.id == role_id for role in getattr(member, "roles", []))

def require_guild(interaction):
    return interaction.guild_id is not None

def parse_positive(value, label):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必須是正整數。")
    if n <= 0:
        raise ValueError(f"{label}必須大於 0。")
    return n

def money_amount(token_amount, tokens_per_unit):
    return token_amount // tokens_per_unit

def safe_calculate(expression):
    """只允許數字與四則運算，避免執行任意 Python 程式。"""
    if len(expression) > 100:
        raise ValueError("算式最多 100 個字元。")
    tree = ast.parse(expression, mode="eval")
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
    def visit(node):
        if isinstance(node, ast.Expression): return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool): return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand); return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            left, right = visit(node.left), visit(node.right)
            if abs(left) > 10**12 or abs(right) > 10**12: raise ValueError("數字太大。")
            return ops[type(node.op)](left, right)
        raise ValueError("只支援數字、+、-、*、/、//、% 與括號。")
    result = visit(tree)
    if not isinstance(result, (int, float)) or abs(result) > 10**12: raise ValueError("結果超出安全範圍。")
    return result

class TokenBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        if GUILD_ID:
            synced = await self.tree.sync(guild=discord.Object(id=GUILD_ID))
            print(f"已同步伺服器指令：{len(synced)}")
        else:
            synced = await self.tree.sync()
            print(f"已同步全域指令：{len(synced)}")

bot = TokenBot()

def command_guild():
    return {"guild": discord.Object(id=GUILD_ID)} if GUILD_ID else {}

@bot.tree.command(name="說明", description="查看代幣、抽獎、匯率與兌換功能", **command_guild())
async def help_cmd(interaction: discord.Interaction):
    text = (
        "**代幣機器人使用說明**\n"
        "`/代幣` 查看自己的伺服器代幣。\n"
        "`/匯率` 查看本伺服器匯率。\n"
        "`/兌換試算` 試算代幣可換多少遊戲幣。\n"
        "`/兌換申請` 建立申請；主辦方在外部付款／發幣後使用 `/兌換完成`。\n"
        "管理員可用 `/抽獎設定` 指定本伺服器可參加抽獎的身分組，`/抽獎設定查看` 可查看設定。\n"
        "`/抽獎開始` 建立抽獎貼文，使用按鈕參加、查看名單、開獎或取消；`/抽獎列表` 可查編號；`/抽獎加入` 可由主辦方直接加入成員。\n"
        "`/捐贈` 可自選代幣捐入抽獎池，降低主辦方外部付款成本。\n"
        "代幣僅是本伺服器內的活動點數，不具有現金價值，不可提現。"
    )
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="計算", description="安全計算簡單數學算式", **command_guild())
async def calculate_cmd(interaction: discord.Interaction, 算式: str):
    try:
        result = safe_calculate(算式.replace("×", "*").replace("÷", "/"))
    except (ValueError, SyntaxError, ZeroDivisionError):
        await interaction.response.send_message("算式無效；只支援數字、括號與 + - * / // %。", ephemeral=True)
        return
    await interaction.response.send_message(f"`{算式}` = **{result:g}**", ephemeral=True)

@bot.tree.command(name="抽獎設定", description="設定本伺服器可參加抽獎的身分組", **command_guild())
@app_commands.describe(參加身分組="只有擁有此身分組的成員可以按參加或被加入抽獎池")
async def lottery_settings(interaction: discord.Interaction, 參加身分組: discord.Role):
    if not is_admin(interaction):
        await interaction.response.send_message("只有伺服器管理員可以設定抽獎參加身分組。", ephemeral=True); return
    with DB_LOCK, db() as c:
        c.execute("INSERT INTO guild_settings(guild_id,entry_role_id,updated_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET entry_role_id=excluded.entry_role_id,updated_by=excluded.updated_by,updated_at=excluded.updated_at", (interaction.guild_id, 參加身分組.id, interaction.user.id, datetime.now(timezone.utc).isoformat()))
    await interaction.response.send_message(f"本伺服器抽獎參加身分組已設定為 {參加身分組.mention}。其他伺服器可各自使用 `/抽獎設定`，互不影響。", ephemeral=True)

@bot.tree.command(name="抽獎設定查看", description="查看本伺服器的抽獎參加身分組", **command_guild())
async def lottery_settings_view(interaction: discord.Interaction):
    role_id = get_entry_role_id(interaction.guild_id)
    text = "未限制參加身分組，所有非機器人會員都可以參加。" if not role_id else f"本伺服器目前只有 <@&{role_id}> 可以參加抽獎。"
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="代幣", description="查看自己或指定成員的代幣餘額", **command_guild())
@app_commands.describe(成員="可選；管理員可查詢其他成員")
async def balance_cmd(interaction: discord.Interaction, 成員: discord.Member | None = None):
    target = 成員 or interaction.user
    if 成員 and target.id != interaction.user.id and not is_admin(interaction):
        await interaction.response.send_message("只有伺服器管理員可以查看其他成員餘額。", ephemeral=True)
        return
    with DB_LOCK, db() as c:
        row = c.execute("SELECT tokens FROM balances WHERE guild_id=? AND user_id=?", (interaction.guild_id, target.id)).fetchone()
    await interaction.response.send_message(f"{target.mention} 目前有 **{row['tokens'] if row else 0}** 枚代幣。", ephemeral=True)

@bot.tree.command(name="匯率", description="查看本伺服器獨立匯率清單", **command_guild())
async def rate_cmd(interaction: discord.Interaction):
    with DB_LOCK, db() as c:
        rows = c.execute("SELECT currency, tokens_per_unit FROM rates WHERE guild_id=? ORDER BY currency", (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message("本伺服器尚未設定匯率，請管理員使用 `/匯率設定`。", ephemeral=True)
        return
    lines = [f"`{r['currency']}`：{r['tokens_per_unit']} 代幣 = 1 單位遊戲幣" for r in rows]
    await interaction.response.send_message("**本伺服器匯率**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="匯率設定", description="管理員設定本伺服器匯率，不影響其他伺服器", **command_guild())
@app_commands.describe(貨幣="例如 gold、gem、coin", 代幣數="多少代幣兌換 1 單位遊戲幣")
async def rate_set(interaction: discord.Interaction, 貨幣: str, 代幣數: int):
    if not is_admin(interaction):
        await interaction.response.send_message("只有伺服器管理員可以編輯匯率。", ephemeral=True); return
    currency = 貨幣.strip().lower()[:32]
    try: tokens = parse_positive(代幣數, "代幣數")
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True); return
    with DB_LOCK, db() as c:
        c.execute("INSERT INTO rates(guild_id,currency,tokens_per_unit) VALUES(?,?,?) ON CONFLICT(guild_id,currency) DO UPDATE SET tokens_per_unit=excluded.tokens_per_unit", (interaction.guild_id, currency, tokens))
    await interaction.response.send_message(f"已設定本伺服器匯率：{tokens} 代幣 = 1 {currency}。", ephemeral=True)

@bot.tree.command(name="匯率刪除", description="管理員刪除本伺服器匯率", **command_guild())
async def rate_delete(interaction: discord.Interaction, 貨幣: str):
    if not is_admin(interaction):
        await interaction.response.send_message("只有伺服器管理員可以刪除匯率。", ephemeral=True); return
    with DB_LOCK, db() as c:
        result = c.execute("DELETE FROM rates WHERE guild_id=? AND currency=?", (interaction.guild_id, 貨幣.strip().lower())).rowcount
    await interaction.response.send_message("匯率已刪除。" if result else "找不到這個匯率。", ephemeral=True)

@bot.tree.command(name="代幣發放", description="主辦方發放或扣除成員代幣", **command_guild())
@app_commands.describe(成員="目標成員", 數量="正數發放；負數扣除", 原因="活動原因")
async def grant_cmd(interaction: discord.Interaction, 成員: discord.Member, 數量: int, 原因: str = "活動發放"):
    if not is_organizer(interaction):
        await interaction.response.send_message("只有伺服器管理員或設定的主辦方角色可以發放代幣。", ephemeral=True); return
    if 數量 == 0 or abs(數量) > 1_000_000:
        await interaction.response.send_message("數量不可為 0，且絕對值不可超過 1,000,000。", ephemeral=True); return
    with DB_LOCK, db() as c:
        row = c.execute("SELECT tokens FROM balances WHERE guild_id=? AND user_id=?", (interaction.guild_id, 成員.id)).fetchone()
        current = row["tokens"] if row else 0
        if current + 數量 < 0:
            await interaction.response.send_message("扣除後不能低於 0。", ephemeral=True); return
        c.execute("INSERT INTO balances(guild_id,user_id,tokens) VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET tokens=?", (interaction.guild_id, 成員.id, current + 數量, current + 數量))
    await interaction.response.send_message(f"已將 {數量:+d} 枚代幣套用至 {成員.mention}；原因：{原因[:100]}。", ephemeral=True)

class LotteryView(discord.ui.View):
    def __init__(self, lottery_id):
        super().__init__(timeout=None)
        self.lottery_id = lottery_id

    @discord.ui.button(label="參加", style=discord.ButtonStyle.success, custom_id="token_lottery_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_enter_lottery(interaction.guild_id, interaction.user):
            await interaction.response.send_message("你沒有指定的抽獎參加身分組，無法加入抽獎池。", ephemeral=True); return
        with DB_LOCK, db() as c:
            lottery = c.execute("SELECT * FROM lotteries WHERE id=? AND guild_id=?", (self.lottery_id, interaction.guild_id)).fetchone()
            if not lottery or lottery["status"] != "open":
                await interaction.response.send_message("這個抽獎已經結束或取消。", ephemeral=True); return
            c.execute("INSERT OR IGNORE INTO lottery_entries VALUES(?,?,?,?,?)", (self.lottery_id, interaction.guild_id, interaction.user.id, interaction.user.id, datetime.now(timezone.utc).isoformat()))
        await interaction.response.send_message("你已加入抽獎池。若要退出，請聯絡主辦方處理。", ephemeral=True)

    @discord.ui.button(label="名單", style=discord.ButtonStyle.secondary, custom_id="token_lottery_list")
    async def listing(self, interaction: discord.Interaction, button: discord.ui.Button):
        with DB_LOCK, db() as c:
            rows = c.execute("SELECT user_id FROM lottery_entries WHERE lottery_id=? ORDER BY created_at", (self.lottery_id,)).fetchall()
        if not rows:
            text = "目前還沒有參加者。"
        else:
            text = "目前參加者（" + str(len(rows)) + " 人）：\n" + " ".join(f"<@{r['user_id']}>" for r in rows[:80])
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="開獎", style=discord.ButtonStyle.primary, custom_id="token_lottery_draw")
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_organizer(interaction):
            await interaction.response.send_message("只有主辦方或管理員可以開獎。", ephemeral=True); return
        result = await draw_lottery(self.lottery_id, interaction.guild_id)
        await interaction.response.send_message(result)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.danger, custom_id="token_lottery_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.Button):
        if not is_organizer(interaction):
            await interaction.response.send_message("只有主辦方或管理員可以取消抽獎。", ephemeral=True); return
        with DB_LOCK, db() as c:
            result = c.execute("UPDATE lotteries SET status='cancelled' WHERE id=? AND guild_id=? AND status='open'", (self.lottery_id, interaction.guild_id)).rowcount
        if not result:
            await interaction.response.send_message("抽獎已經結束或取消。", ephemeral=True); return
        await interaction.response.edit_message(content="已取消", embed=None, view=None)

async def draw_lottery(lottery_id, guild_id):
    with DB_LOCK, db() as c:
        lottery = c.execute("SELECT * FROM lotteries WHERE id=? AND guild_id=?", (lottery_id, guild_id)).fetchone()
        entries = c.execute("SELECT user_id FROM lottery_entries WHERE lottery_id=?", (lottery_id,)).fetchall()
        if not lottery or lottery["status"] != "open": return "這個抽獎已經結束或取消。"
        if len(entries) < lottery["winners"]: return f"參加人數不足：目前 {len(entries)} 人，需要至少 {lottery['winners']} 人。"
        selected = random.SystemRandom().sample([r["user_id"] for r in entries], lottery["winners"])
        each, remainder = divmod(lottery["pool"], lottery["winners"])
        for i, user_id in enumerate(selected):
            amount = each + (1 if i < remainder else 0)
            c.execute("INSERT INTO balances(guild_id,user_id,tokens) VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET tokens=tokens+excluded.tokens", (guild_id, user_id, amount))
        c.execute("UPDATE lotteries SET status='completed' WHERE id=?", (lottery_id,))
    result = "\n".join(f"<@{user_id}>：{each + (1 if i < remainder else 0)} 枚代幣" for i, user_id in enumerate(selected))
    return f"**抽獎結果 #{lottery_id}**\n{lottery['description'] or ''}\n{result}\n\n代幣沒有現金價值，不可提現。"

@bot.tree.command(name="抽獎開始", description="建立有按鈕參加的代幣抽獎", **command_guild())
@app_commands.describe(代幣總額="本次抽獎初始代幣總額", 得主數="得主數量", 說明="活動說明")
async def lottery_cmd(interaction: discord.Interaction, 代幣總額: int, 得主數: int, 說明: str = ""):
    if not is_organizer(interaction):
        await interaction.response.send_message("只有伺服器管理員或主辦方可以建立抽獎。", ephemeral=True); return
    try: pool, winners = parse_positive(代幣總額, "代幣總額"), parse_positive(得主數, "得主數")
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True); return
    with DB_LOCK, db() as c:
        cur = c.execute("INSERT INTO lotteries(guild_id,organizer_id,pool,winners,description,created_at,status,channel_id) VALUES(?,?,?,?,?,?,?,?)", (interaction.guild_id, interaction.user.id, pool, winners, 說明[:200], datetime.now(timezone.utc).isoformat(), "open", interaction.channel_id))
        lottery_id = cur.lastrowid
    embed = discord.Embed(title=f"🎁 抽獎 #{lottery_id}", description=f"**抽獎編號：** `{lottery_id}`\n**獎池：** {pool} 枚代幣\n**得主數：** {winners} 人\n**說明：** {說明[:200] or '無'}\n\n按下「參加」加入抽獎池；主辦方可直接加入成員或接受代幣捐贈。", color=0xE91E63)
    await interaction.response.send_message(embed=embed, view=LotteryView(lottery_id))
    message = await interaction.original_response()
    with DB_LOCK, db() as c: c.execute("UPDATE lotteries SET message_id=? WHERE id=?", (message.id, lottery_id))

@bot.tree.command(name="抽獎列表", description="查看本伺服器進行中的抽獎編號", **command_guild())
async def lottery_list(interaction: discord.Interaction):
    with DB_LOCK, db() as c:
        rows = c.execute("SELECT id,pool,winners,description FROM lotteries WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 20", (interaction.guild_id,)).fetchall()
        counts = {r["id"]: r["count"] for r in c.execute("SELECT lottery_id,COUNT(*) AS count FROM lottery_entries GROUP BY lottery_id").fetchall()}
    if not rows:
        await interaction.response.send_message("本伺服器目前沒有進行中的抽獎。", ephemeral=True); return
    text = "\n".join(f"`#{r['id']}`：獎池 {r['pool']} 代幣、得主 {r['winners']} 人、參加 {counts.get(r['id'], 0)} 人、{r['description'] or '無說明'}" for r in rows)
    await interaction.response.send_message("**進行中的抽獎**\n" + text, ephemeral=True)

@bot.tree.command(name="抽獎加入", description="主辦方直接把成員加入抽獎池", **command_guild())
async def lottery_add(interaction: discord.Interaction, 抽獎編號: int, 成員: discord.Member):
    if not is_organizer(interaction):
        await interaction.response.send_message("只有主辦方或管理員可以直接加入成員。", ephemeral=True); return
    if not can_enter_lottery(interaction.guild_id, 成員):
        await interaction.response.send_message("該成員沒有指定的抽獎參加身分組，不能加入抽獎池。", ephemeral=True); return
    with DB_LOCK, db() as c:
        lottery = c.execute("SELECT status FROM lotteries WHERE id=? AND guild_id=?", (抽獎編號, interaction.guild_id)).fetchone()
        if not lottery or lottery["status"] != "open": msg = "找不到進行中的抽獎。"
        else:
            c.execute("INSERT OR IGNORE INTO lottery_entries VALUES(?,?,?,?,?)", (抽獎編號, interaction.guild_id, 成員.id, interaction.user.id, datetime.now(timezone.utc).isoformat()))
            msg = f"已將 {成員.mention} 加入抽獎 #{抽獎編號}。"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="捐贈", description="自選代幣捐入抽獎池，降低主辦方外部成本", **command_guild())
async def lottery_donate(interaction: discord.Interaction, 抽獎編號: int, 數量: int):
    try: amount = parse_positive(數量, "捐贈數量")
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True); return
    with DB_LOCK, db() as c:
        lottery = c.execute("SELECT status FROM lotteries WHERE id=? AND guild_id=?", (抽獎編號, interaction.guild_id)).fetchone()
        balance = c.execute("SELECT tokens FROM balances WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id)).fetchone()
        if not lottery or lottery["status"] != "open": msg = "找不到進行中的抽獎。"
        elif not balance or balance["tokens"] < amount: msg = "你的代幣餘額不足，捐贈未完成。"
        else:
            c.execute("UPDATE balances SET tokens=tokens-? WHERE guild_id=? AND user_id=?", (amount, interaction.guild_id, interaction.user.id))
            c.execute("UPDATE lotteries SET pool=pool+? WHERE id=?", (amount, 抽獎編號))
            msg = f"已捐贈 {amount} 枚代幣至抽獎 #{抽獎編號}；獎池增加 {amount}。"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="兌換試算", description="試算代幣可兌換多少遊戲幣", **command_guild())
async def calc_exchange(interaction: discord.Interaction, 貨幣: str, 代幣數: int):
    try: amount = parse_positive(代幣數, "代幣數")
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True); return
    with DB_LOCK, db() as c:
        row = c.execute("SELECT tokens_per_unit FROM rates WHERE guild_id=? AND currency=?", (interaction.guild_id, 貨幣.strip().lower())).fetchone()
    if not row:
        await interaction.response.send_message("本伺服器沒有這個匯率。", ephemeral=True); return
    game = money_amount(amount, row["tokens_per_unit"])
    await interaction.response.send_message(f"{amount} 枚代幣可兌換 **{game} {貨幣.strip().lower()}**；不足 1 單位的餘數不會自動兌換。", ephemeral=True)

@bot.tree.command(name="兌換申請", description="建立遊戲幣兌換申請，不會自動付款", **command_guild())
async def exchange_request(interaction: discord.Interaction, 貨幣: str, 代幣數: int):
    try: amount = parse_positive(代幣數, "代幣數")
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True); return
    currency = 貨幣.strip().lower()
    with DB_LOCK, db() as c:
        rate = c.execute("SELECT tokens_per_unit FROM rates WHERE guild_id=? AND currency=?", (interaction.guild_id, currency)).fetchone()
        balance = c.execute("SELECT tokens FROM balances WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id)).fetchone()
        if not rate: msg = "本伺服器沒有這個匯率。"
        elif not balance or balance["tokens"] < amount: msg = "代幣餘額不足。"
        else:
            game = money_amount(amount, rate["tokens_per_unit"])
            if game <= 0: msg = "代幣數不足以兌換 1 單位遊戲幣。"
            else:
                cur = c.execute("INSERT INTO exchanges(guild_id,user_id,currency,token_amount,game_amount,created_at) VALUES(?,?,?,?,?,?)", (interaction.guild_id, interaction.user.id, currency, amount, game, datetime.now(timezone.utc).isoformat()))
                msg = f"兌換申請已建立，編號 **{cur.lastrowid}**：{amount} 代幣 → {game} {currency}。代幣會在主辦方使用 `/兌換完成` 後扣除；Bot 不會自動付款。"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="兌換完成", description="主辦方確認外部遊戲幣已支付後完成兌換", **command_guild())
async def exchange_complete(interaction: discord.Interaction, 申請編號: int):
    if not is_organizer(interaction):
        await interaction.response.send_message("只有伺服器管理員或主辦方可以完成兌換。", ephemeral=True); return
    with DB_LOCK, db() as c:
        row = c.execute("SELECT * FROM exchanges WHERE id=? AND guild_id=? AND status='pending'", (申請編號, interaction.guild_id)).fetchone()
        if not row: msg = "找不到待處理的兌換申請。"
        else:
            balance = c.execute("SELECT tokens FROM balances WHERE guild_id=? AND user_id=?", (interaction.guild_id, row["user_id"])).fetchone()
            if not balance or balance["tokens"] < row["token_amount"]: msg = "申請人的代幣餘額已不足，未完成扣除。"
            else:
                c.execute("UPDATE balances SET tokens=tokens-? WHERE guild_id=? AND user_id=?", (row["token_amount"], interaction.guild_id, row["user_id"]))
                c.execute("UPDATE exchanges SET status='completed', completed_by=? WHERE id=?", (interaction.user.id, 申請編號))
                msg = f"兌換申請 **{申請編號}** 已完成，已扣除 {row['token_amount']} 代幣。外部遊戲幣付款由主辦方自行處理。"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="兌換查詢", description="管理員查詢待處理兌換申請", **command_guild())
async def exchange_list(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("只有伺服器管理員可以查詢申請。", ephemeral=True); return
    with DB_LOCK, db() as c:
        rows = c.execute("SELECT id,user_id,currency,token_amount,game_amount,created_at FROM exchanges WHERE guild_id=? AND status='pending' ORDER BY id LIMIT 30", (interaction.guild_id,)).fetchall()
    if not rows: text = "目前沒有待處理申請。"
    else: text = "\n".join(f"#{r['id']} <@{r['user_id']}>：{r['token_amount']} 代幣 → {r['game_amount']} {r['currency']}" for r in rows)
    await interaction.response.send_message(text, ephemeral=True)

@bot.event
async def on_ready():
    with DB_LOCK, db() as c:
        active = c.execute("SELECT id, message_id FROM lotteries WHERE status='open' AND message_id IS NOT NULL LIMIT 100").fetchall()
    for row in active:
        bot.add_view(LotteryView(row["id"]), message_id=row["message_id"])
    print(f"代幣機器人已上線：{bot.user} ({bot.user.id})")

if __name__ == "__main__":
    init_db()
    keep_alive()
    bot.run(TOKEN)
