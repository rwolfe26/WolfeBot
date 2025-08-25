"""
Discord Role Menu Bot
---------------------
Features
- Slash command to create a persistent role menu with a dropdown (select menu)
- Users can add/remove roles by (re)submitting the menu
- Supports up to 25 roles per menu (Discord UI limit)
- Persistent across restarts; menus are re-attached on startup
- Simple JSON storage; no database required

Commands (slash)
- /role_menu create title:"Text" multi:true roles:@Role @Role ...
- /role_menu delete message_link:"https://discord.com/channels/..."

Setup
1) Python 3.10+
2) pip install -U discord.py python-dotenv
3) Create a .env file next to this script containing:
   DISCORD_TOKEN=your_bot_token_here
4) In the Discord Developer Portal:
   - Enable the "Message Content Intent" OFF (not needed)
   - Enable "Server Members Intent" ON (required to manage roles)
   - Invite the bot with the following scope/permissions:
     Scopes: bot, applications.commands
     Bot Permissions: Manage Roles, Read Messages/View Channels, Send Messages, Use External Emojis, Embed Links
5) Run: python main.py

Notes
- The bot must be **above** the roles it assigns in the server's role list.
- Members must not have higher roles than the bot.
"""

from __future__ import annotations
import os
import json
import re
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ------------------ Storage ------------------
STORAGE_FILE = "role_menus.json"

@dataclass
class RoleMenuRecord:
    guild_id: int
    channel_id: int
    message_id: int
    title: str
    role_ids: List[int]
    multi: bool = True


def load_storage() -> Dict[str, RoleMenuRecord]:
    if not os.path.exists(STORAGE_FILE):
        return {}
    with open(STORAGE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[str, RoleMenuRecord] = {}
    for k, v in raw.items():
        out[k] = RoleMenuRecord(**v)
    return out


def save_storage(data: Dict[str, RoleMenuRecord]):
    serializable = {k: asdict(v) for k, v in data.items()}
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2)


storage: Dict[str, RoleMenuRecord] = load_storage()

# ------------------ UI Components ------------------
class RolesSelect(discord.ui.Select):
    def __init__(self, roles: List[discord.Role], multi: bool):
        options = [
            discord.SelectOption(label=role.name, value=str(role.id)) for role in roles
        ]
        max_values = len(options) if multi else 1
        super().__init__(
            placeholder="Choose your roles…",
            min_values=0,
            max_values=max_values,
            options=options,
        )
        self._roles = roles
        self._multi = multi

    async def callback(self, interaction: discord.Interaction):
        assert interaction.user is not None
        member = interaction.guild.get_member(interaction.user.id)  # type: ignore
        if member is None:
            await interaction.response.send_message(
                "Couldn't find your member record.", ephemeral=True
            )
            return

        # Roles managed by this menu
        menu_role_ids = {int(v) for v in [o.value for o in self.options]}
        selected_ids = {int(v) for v in self.values}

        # Remove all menu roles then add selected
        roles_to_remove = [r for r in member.roles if r.id in menu_role_ids]
        roles_to_add = [member.guild.get_role(rid) for rid in selected_ids]
        roles_to_add = [r for r in roles_to_add if r is not None]

        try:
            # Remove first
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Role menu update")
            # Then add
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Role menu update")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I lack permission to manage one or more of those roles.\n"
                "Make sure my bot role is above the roles I'm assigning.",
                ephemeral=True,
            )
            return

        if selected_ids:
            chosen = ", ".join([r.name for r in roles_to_add])
            await interaction.response.send_message(
                f"Updated! You now have: **{chosen}**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Removed this menu's roles from you.", ephemeral=True
            )


class RoleMenuView(discord.ui.View):
    def __init__(self, record: RoleMenuRecord, guild: discord.Guild):
        super().__init__(timeout=None)  # persistent
        roles = [guild.get_role(rid) for rid in record.role_ids]
        roles = [r for r in roles if r is not None]
        self.add_item(RolesSelect(roles, multi=record.multi))


# ------------------ Bot ------------------
intents = discord.Intents.default()
intents.message_content = False
intents.members = True  # required for role management

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN not found in environment (.env)")

bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)

def record_key(guild_id: int, message_id: int) -> str:
    return f"{guild_id}:{message_id}"


@bot.event
async def on_ready():
    # Re-attach persistent views for existing menus
    for key, rec in storage.items():
        guild = bot.get_guild(rec.guild_id)
        if guild is None:
            continue
        view = RoleMenuView(rec, guild)
        try:
            bot.add_view(view, message_id=rec.message_id)
        except Exception:
            pass
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# -------------- Helpers --------------
ROLE_MENTION_RE = re.compile(r"<@&(?P<id>\d+)>")


def parse_role_mentions(text: str) -> List[int]:
    return [int(m.group("id")) for m in ROLE_MENTION_RE.finditer(text)]


# -------------- Slash Commands --------------
class RoleMenuGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="role_menu", description="Create and manage role menus")

    @app_commands.command(name="create", description="Create a role select menu in this channel")
    @app_commands.describe(
        title="Title above the dropdown",
        roles="Mention the roles (up to 25) you want in the menu",
        multi="Allow picking multiple roles? (default: true)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        title: str,
        roles: str,
        multi: Optional[bool] = True,
    ):
        if not interaction.user or not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        # Permissions check
        me = interaction.guild.me
        if not me.guild_permissions.manage_roles:  # type: ignore
            await interaction.response.send_message(
                "I need the **Manage Roles** permission.", ephemeral=True
            )
            return

        role_ids = parse_role_mentions(roles)
        if not role_ids:
            await interaction.response.send_message(
                "Please **mention** the roles you want in the menu.", ephemeral=True
            )
            return
        if len(role_ids) > 25:
            await interaction.response.send_message(
                "Discord limits select menus to 25 options.", ephemeral=True
            )
            return

        # Validate roles are assignable
        resolved_roles: List[discord.Role] = []
        for rid in role_ids:
            r = interaction.guild.get_role(rid)
            if r is None:
                continue
            if r >= interaction.guild.me.top_role:  # type: ignore
                await interaction.response.send_message(
                    f"Role **{r.name}** is higher or equal to my top role. Move me above it.",
                    ephemeral=True,
                )
                return
            resolved_roles.append(r)

        embed = discord.Embed(title=title, description="Use the dropdown to pick your roles.")
        view = RoleMenuView(
            RoleMenuRecord(
                guild_id=interaction.guild.id,
                channel_id=interaction.channel_id,
                message_id=0,
                title=title,
                role_ids=[r.id for r in resolved_roles],
                multi=bool(multi),
            ),
            interaction.guild,
        )

        await interaction.response.defer(ephemeral=False, thinking=True)
        msg = await interaction.channel.send(embed=embed, view=view)

        # Save
        rec = RoleMenuRecord(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            message_id=msg.id,
            title=title,
            role_ids=[r.id for r in resolved_roles],
            multi=bool(multi),
        )
        storage[record_key(rec.guild_id, rec.message_id)] = rec
        save_storage(storage)

        await interaction.followup.send(
            f"Role menu created: {msg.jump_url}", ephemeral=True
        )

    @app_commands.command(name="delete", description="Delete a role menu by message link" )
    @app_commands.describe(message_link="Paste the message link (Copy Message Link)")
    async def delete(self, interaction: discord.Interaction, message_link: str):
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return

        m = re.search(r"/channels/(\d+)/(\d+)/(\d+)$", message_link)
        if not m:
            await interaction.response.send_message("Invalid message link.", ephemeral=True)
            return
        g_id, c_id, msg_id = map(int, m.groups())
        if g_id != interaction.guild.id:
            await interaction.response.send_message("That message isn't in this server.", ephemeral=True)
            return

        key = record_key(g_id, msg_id)
        if key not in storage:
            await interaction.response.send_message("No role menu found for that message.", ephemeral=True)
            return

        # Attempt to delete the message
        channel = interaction.guild.get_channel(c_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                await interaction.response.send_message("I can't delete that message, but the menu is unregistered.", ephemeral=True)

        # Remove from storage
        storage.pop(key, None)
        save_storage(storage)
        await interaction.response.send_message("Role menu removed.", ephemeral=True)


bot.tree.add_command(RoleMenuGroup())

# Sync on startup for convenience (optional)
@bot.event
async def setup_hook():
    # Global sync; you can change to per-guild for faster iteration
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"Slash sync failed: {e}")


if __name__ == "__main__":
    bot.run(TOKEN)
