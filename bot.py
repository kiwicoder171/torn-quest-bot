import logging
import os
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

from flight_club import FlightClubClient
from key_pool import KeyPool, SAFE_DEFAULT_MAX_RPM, SAFE_MAX_RPM, SAFE_MIN_RPM
from torn_api import TornApiClient
from watcher import QuestDeal, QuestWatcher

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("TornQuestBot")

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_RPM = int(os.getenv("DEFAULT_KEY_MAX_RPM", str(SAFE_DEFAULT_MAX_RPM)))
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL_SECONDS", "3.0"))


class TornQuestBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.key_pool = KeyPool(storage_path=DATA_DIR / "keys.json")
        self.flight_club = FlightClubClient()
        self.torn_api = TornApiClient()
        self.watcher = QuestWatcher(
            key_pool=self.key_pool,
            flight_club=self.flight_club,
            torn_api=self.torn_api,
            storage_path=DATA_DIR / "quest_state.json",
            scan_interval=SCAN_INTERVAL,
            on_deal=self.handle_deal,
            on_notice=self.handle_notice,
        )

    async def setup_hook(self) -> None:
        synced = await self.tree.sync()
        logger.info("Synced %d global command(s): %s", len(synced), [c.name for c in synced])
        if self.watcher.watching and self.watcher.alert_channel_id:
            self.watcher.start(self.watcher.alert_channel_id)

    async def close(self) -> None:
        self.watcher.stop()
        await self.flight_club.close()
        await self.torn_api.close()
        await super().close()

    async def handle_deal(self, deal: QuestDeal) -> None:
        channel_id = self.watcher.alert_channel_id
        if not channel_id:
            return
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        pings = " ".join(f"<@{uid}>" for uid in sorted(self.watcher.subscribers))
        is_profit = deal.profit_per_unit >= 0
        color = discord.Color.green() if is_profit else discord.Color.gold()
        diff_label = (
            f"+${deal.profit_per_unit:,}/ea (+${deal.total_profit:,} total)"
            if is_profit
            else f"-${abs(deal.profit_per_unit):,}/ea over TFC"
        )

        embed = discord.Embed(
            title=f"🎯 Quest Item Match: {deal.item_name} (x{deal.amount})",
            url=deal.market_url,
            color=color,
        )
        embed.add_field(name="Market Price", value=f"**${deal.listed_price:,}**", inline=True)
        embed.add_field(name="TFC Buyout", value=f"${deal.fc_value:,}", inline=True)
        embed.add_field(name="Margin vs TFC", value=diff_label, inline=True)
        embed.add_field(name="TFC Recipient", value=f"`{deal.recipient}`", inline=True)
        if deal.contest_points is not None:
            embed.add_field(name="Team Points", value=f"{deal.contest_points} pts/ea", inline=True)
        if deal.nfs_rank is not None:
            embed.add_field(name="Daily NFS", value=f"Rank #{deal.nfs_rank}", inline=True)
        embed.set_footer(text="Click title to open Torn Item Market • Manual buy only")

        await channel.send(content=pings or None, embed=embed)

    async def handle_notice(self, message: str) -> None:
        channel_id = self.watcher.alert_channel_id
        if not channel_id:
            return
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(message)


bot = TornQuestBot()


@bot.tree.command(name="help", description="Show all TornQuestBot commands and how the team watcher works.")
async def help_command(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🧭 TornQuestBot — Command Guide",
        description=(
            "Watches Torn's Item Market for your team's Daily Quest & NFS items and pings "
            "everyone whenever a listing is **≤ TFC buyout price**."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🔑 API Key Pool (Private / Ephemeral)",
        value=(
            "`/setkey <api_key> [max_rpm]` — Save & activate your Torn API key (default 35 req/min, max 50)\n"
            "`/stopkey` — Immediately pause your key from being used\n"
            "`/startkey` — Re-activate your paused key\n"
            "`/removekey` — Delete your key from the bot"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎯 Quest & Daily NFS Items",
        value=(
            "`/quest add <item> [max_overpay_pct]` — Add a quest item (with TFC autocomplete)\n"
            "`/quest nfs [max_overpay_pct]` — Auto-add all active Daily NFS items from TFC\n"
            "`/quest remove <item>` — Remove an item from the watch list\n"
            "`/quest clear` — Clear all watched items\n"
            "`/quest list` — View watched items & TFC price ceilings"
        ),
        inline=False,
    )
    embed.add_field(
        name="📡 Watcher & Team Pings",
        value=(
            "`/watch start` — Start watching in this channel using active pool keys\n"
            "`/watch stop` — Pause the market watcher\n"
            "`/watch status` — View watcher state, active/paused keys, and RPM usage\n"
            "`/notify <True|False>` — Opt in/out of `@mention` pings on deal alerts"
        ),
        inline=False,
    )
    embed.set_footer(text="Rate-limit safe: per-key cap ≤ 50 req/min • Load-balanced across active keys • Manual buy only")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="setkey", description="Register your Torn API key (private) with safe rate limiting.")
@app_commands.describe(
    api_key="Your Torn Public API key",
    max_rpm=f"Max requests/min for your key ({SAFE_MIN_RPM}-{SAFE_MAX_RPM}, default {DEFAULT_RPM})",
)
async def setkey(
    interaction: discord.Interaction,
    api_key: str,
    max_rpm: Optional[int] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    ok, msg, profile = await bot.torn_api.verify_key(api_key.strip())
    if not ok or not profile:
        await interaction.followup.send(f"❌ Key verification failed: {msg}", ephemeral=True)
        return

    rpm = max_rpm if max_rpm is not None else DEFAULT_RPM
    entry = bot.key_pool.set_key(
        discord_id=interaction.user.id,
        torn_name=profile["name"],
        torn_id=profile["id"],
        api_key=api_key.strip(),
        max_rpm=rpm,
    )
    bot.watcher.set_subscriber(interaction.user.id, True)
    await interaction.followup.send(
        f"✅ Saved & activated key for **{entry.torn_name} [{entry.torn_id}]** "
        f"(capped at **{entry.max_rpm} req/min** so you keep {100 - entry.max_rpm}+ req/min headroom).\n"
        f"You are also subscribed to team deal pings (`/notify off` to mute).",
        ephemeral=True,
    )


@bot.tree.command(name="stopkey", description="Immediately stop the bot from using your API key.")
async def stopkey(interaction: discord.Interaction) -> None:
    entry = bot.key_pool.pause_key(interaction.user.id, reason="Stopped by user")
    if not entry:
        await interaction.response.send_message("ℹ️ You don't have a registered API key.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"⏸️ Stopped using your API key (**{entry.torn_name}**). "
        f"Active pool keys remaining: **{bot.key_pool.active_count()}**.\n"
        f"Use `/startkey` anytime to re-enable it.",
        ephemeral=True,
    )


@bot.tree.command(name="startkey", description="Re-activate your stored Torn API key for the team pool.")
async def startkey(interaction: discord.Interaction) -> None:
    entry = bot.key_pool.resume_key(interaction.user.id)
    if not entry:
        await interaction.response.send_message(
            "ℹ️ No stored key found. Use `/setkey` to register one.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"▶️ Re-activated your API key (**{entry.torn_name}**, cap **{entry.max_rpm} req/min**). "
        f"Active pool keys: **{bot.key_pool.active_count()}**.",
        ephemeral=True,
    )


@bot.tree.command(name="removekey", description="Delete your Torn API key from the bot completely.")
async def removekey(interaction: discord.Interaction) -> None:
    removed = bot.key_pool.remove_key(interaction.user.id)
    if not removed:
        await interaction.response.send_message("ℹ️ You don't have a stored API key.", ephemeral=True)
        return
    await interaction.response.send_message("🗑️ Your API key has been deleted.", ephemeral=True)


@bot.tree.command(name="notify", description="Toggle whether you get pinged when a quest deal is found.")
@app_commands.describe(enabled="True to receive @mention pings, False to mute")
async def notify(interaction: discord.Interaction, enabled: bool) -> None:
    bot.watcher.set_subscriber(interaction.user.id, enabled)
    state = "🔔 Subscribed to" if enabled else "🔕 Muted"
    await interaction.response.send_message(f"{state} team quest deal pings.", ephemeral=True)


quest_group = app_commands.Group(name="quest", description="Manage today's team daily quest items.")


async def item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    matches = await bot.flight_club.search_items(current, limit=25)
    return [
        app_commands.Choice(
            name=f"{it.item_name} (${it.fc_value:,})"[:100],
            value=str(it.item_id),
        )
        for it in matches
    ]


@quest_group.command(name="add", description="Add a daily quest item to watch on the Item Market.")
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.describe(
    item="Item name or ID",
    max_overpay_pct="Optional % above TFC price to still alert (default 0%)",
)
async def quest_add(
    interaction: discord.Interaction,
    item: str,
    max_overpay_pct: float = 0.0,
) -> None:
    await interaction.response.defer()
    tfc_item = await bot.flight_club.find_item(item)
    if not tfc_item:
        await interaction.followup.send(f"❌ Item `{item}` not found in TFC item list.")
        return

    qi = bot.watcher.add_quest_item(tfc_item.item_id, tfc_item.item_name, max_overpay_pct)
    target = int(tfc_item.fc_value * (1.0 + qi.max_overpay_pct / 100.0))
    await interaction.followup.send(
        f"✅ Added **{tfc_item.item_name}** (`#{tfc_item.item_id}`) — "
        f"TFC Price: **${tfc_item.fc_value:,}** | Alert ceiling: **${target:,}** (`+{qi.max_overpay_pct:.1f}%`)"
    )


@quest_group.command(name="nfs", description="Auto-add all of today's Daily NFS items from TFC.")
@app_commands.describe(max_overpay_pct="Optional % above TFC price to still alert (default 0%)")
async def quest_nfs(interaction: discord.Interaction, max_overpay_pct: float = 0.0) -> None:
    await interaction.response.defer()
    all_items = await bot.flight_club.get_all_items()
    nfs_items = sorted(
        (it for it in all_items.values() if it.nfs_rank is not None),
        key=lambda it: (it.nfs_rank or 99, it.item_name),
    )
    if not nfs_items:
        await interaction.followup.send("ℹ️ No active Daily NFS items found on TFC right now.")
        return

    lines = []
    for it in nfs_items:
        qi = bot.watcher.add_quest_item(it.item_id, it.item_name, max_overpay_pct)
        target = int(it.fc_value * (1.0 + qi.max_overpay_pct / 100.0))
        lines.append(
            f"• **Rank #{it.nfs_rank}** — **{it.item_name}** (`#{it.item_id}`): TFC `${it.fc_value:,}` | Ceiling `${target:,}`"
        )

    embed = discord.Embed(
        title=f"⚡ Added {len(nfs_items)} Daily NFS Item(s)",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    await interaction.followup.send(embed=embed)


@quest_group.command(name="remove", description="Remove an item from the daily quest watch list.")
@app_commands.autocomplete(item=item_autocomplete)
async def quest_remove(interaction: discord.Interaction, item: str) -> None:
    tfc_item = await bot.flight_club.find_item(item)
    if not tfc_item or not bot.watcher.remove_quest_item(tfc_item.item_id):
        await interaction.response.send_message(f"ℹ️ `{item}` is not in the active quest list.")
        return
    await interaction.response.send_message(f"🗑️ Removed **{tfc_item.item_name}** from the quest list.")


@quest_group.command(name="clear", description="Clear all daily quest items.")
async def quest_clear(interaction: discord.Interaction) -> None:
    count = bot.watcher.clear_quest_items()
    await interaction.response.send_message(f"🧹 Cleared **{count}** quest item(s).")


@quest_group.command(name="list", description="Show active daily quest items and their TFC target prices.")
async def quest_list(interaction: discord.Interaction) -> None:
    if not bot.watcher.quest_items:
        await interaction.response.send_message("📭 No daily quest items set. Use `/quest add` to add some.")
        return

    lines = []
    for qi in bot.watcher.quest_items.values():
        tfc = await bot.flight_club.get_item(qi.item_id)
        fc_val = tfc.fc_value if tfc else 0
        target = int(fc_val * (1.0 + qi.max_overpay_pct / 100.0))
        lines.append(f"• **{qi.item_name}** (`#{qi.item_id}`) — TFC: `${fc_val:,}` | Ceiling: `${target:,}`")

    embed = discord.Embed(
        title="📋 Active Team Quest Items",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


watch_group = app_commands.Group(name="watch", description="Start, stop, or inspect the team market watcher.")


@watch_group.command(name="start", description="Activate the market watcher in this channel using active pool keys.")
async def watch_start(interaction: discord.Interaction) -> None:
    bot.watcher.set_subscriber(interaction.user.id, True)
    if bot.key_pool.active_count() == 0:
        await interaction.response.send_message(
            "⚠️ No active API keys in the pool! At least one teammate needs `/setkey` or `/startkey` first.",
            ephemeral=True,
        )
        return
    if not bot.watcher.quest_items:
        await interaction.response.send_message(
            "⚠️ No quest items configured yet. Add items with `/quest add`, then run `/watch start`."
        )
        return

    bot.watcher.start(interaction.channel_id)
    await interaction.response.send_message(
        f"🚀 **Team Quest Watcher activated** in <#{interaction.channel_id}>!\n"
        f"• Active keys in pool: **{bot.key_pool.active_count()}** "
        f"(total safe capacity: **{bot.key_pool.total_rpm_capacity()} req/min**)\n"
        f"• Watching **{len(bot.watcher.quest_items)}** item(s) • Subscribers: **{len(bot.watcher.subscribers)}**"
    )


@watch_group.command(name="stop", description="Pause the market watcher for the team.")
async def watch_stop(interaction: discord.Interaction) -> None:
    bot.watcher.stop()
    await interaction.response.send_message("🛑 **Team Quest Watcher paused.** Any teammate can run `/watch start` to resume.")


@watch_group.command(name="status", description="Show watcher state, active pool keys (no secrets), and RPM usage.")
async def watch_status(interaction: discord.Interaction) -> None:
    entries = bot.key_pool.list_entries()
    key_lines = []
    for e in entries:
        state_icon = "🟢 Active" if e.active else "⏸️ Paused"
        used = e.limiter.used_calls()
        key_lines.append(
            f"• **{e.torn_name}** (<@{e.discord_id}>): {state_icon} — `{used}/{e.limiter.max_calls} req/min` (total: {e.calls_made})"
        )

    embed = discord.Embed(
        title="📡 Team Quest Watcher Status",
        color=discord.Color.green() if bot.watcher.watching else discord.Color.light_grey(),
    )
    embed.add_field(name="Watcher", value="🟢 Running" if bot.watcher.watching else "🛑 Stopped", inline=True)
    embed.add_field(name="Quest Items", value=str(len(bot.watcher.quest_items)), inline=True)
    embed.add_field(name="Subscribers", value=str(len(bot.watcher.subscribers)), inline=True)
    embed.add_field(
        name="API Key Pool",
        value="\n".join(key_lines) if key_lines else "No keys registered.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(quest_group)
bot.tree.add_command(watch_group)


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and add your bot token.")
    bot.run(token)


if __name__ == "__main__":
    main()
