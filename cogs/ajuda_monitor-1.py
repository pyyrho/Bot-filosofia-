"""Menções administrativas e acompanhamento de dúvidas em canais de ajuda.

O fluxo de inatividade foi inspirado no sistema ``clopen`` do projeto
discord-math/bot: uma dúvida fica vinculada ao autor original, a atividade do
canal reinicia o prazo e somente o autor pode responder à verificação.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.storage import store

log = logging.getLogger("cogs.ajuda_monitor")

NAMESPACE = "help_monitor"
CONFIG_KEY = "config"
SESSIONS_KEY = "sessions"
DEFAULT_INACTIVITY_MINUTES = 30
MIN_INACTIVITY_MINUTES = 5
MAX_INACTIVITY_MINUTES = 24 * 60
CHECK_INTERVAL_SECONDS = 15
CHECK_MARK = "✅"
CROSS_MARK = "❌"


def _default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "mentor_role_id": None,
        "channel_ids": [],
        "inactivity_minutes": DEFAULT_INACTIVITY_MINUTES,
    }


def _normalize_config(raw: Any) -> dict[str, Any]:
    config = _default_config()
    if not isinstance(raw, dict):
        return config

    role_id = raw.get("mentor_role_id")
    if isinstance(role_id, int) and role_id > 0:
        config["mentor_role_id"] = role_id

    channel_ids = raw.get("channel_ids", [])
    if isinstance(channel_ids, list):
        config["channel_ids"] = sorted(
            {
                channel_id
                for channel_id in channel_ids
                if isinstance(channel_id, int) and channel_id > 0
            }
        )

    minutes = raw.get("inactivity_minutes", DEFAULT_INACTIVITY_MINUTES)
    if isinstance(minutes, int):
        config["inactivity_minutes"] = max(
            MIN_INACTIVITY_MINUTES,
            min(minutes, MAX_INACTIVITY_MINUTES),
        )

    config["enabled"] = bool(raw.get("enabled", False))
    return config


def _normalize_sessions(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}

    sessions: dict[str, dict[str, Any]] = {}
    for channel_key, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            raw_channel_id = value.get("channel_id", str(channel_key).split(":", 1)[0])
            channel_id = int(raw_channel_id)
            owner_id = int(value["owner_id"])
            origin_message_id = int(value["origin_message_id"])
            last_activity = float(value["last_activity"])
            prompt_message_id = value.get("prompt_message_id")
            if prompt_message_id is not None:
                prompt_message_id = int(prompt_message_id)
        except (KeyError, TypeError, ValueError):
            continue
        if channel_id <= 0 or owner_id <= 0 or origin_message_id <= 0:
            continue
        sessions[_session_key(channel_id, owner_id)] = {
            "channel_id": channel_id,
            "owner_id": owner_id,
            "origin_message_id": origin_message_id,
            "last_activity": last_activity,
            "prompt_message_id": prompt_message_id,
            "mentor_pinged": bool(value.get("mentor_pinged", False)),
        }
    return sessions


def _session_key(channel_id: int, owner_id: int) -> str:
    return f"{channel_id}:{owner_id}"


def _looks_like_question(content: str, *, has_attachment: bool = False) -> bool:
    """Evita iniciar sessões com cumprimentos e conversa curta aleatória."""
    if has_attachment:
        return True

    text = content.strip().casefold()
    if len(text) < 3:
        return False
    if "?" in text:
        return True

    question_terms = (
        "ajuda",
        "alguém",
        "duvida",
        "dúvida",
        "questão",
        "questao",
        "problema",
        "resolver",
        "resolva",
        "calcular",
        "calcule",
        "como faço",
        "como faz",
        "por que",
        "porque",
        "help",
        "someone",
    )
    math_markers = ("=", "√", "∫", "∑", "lim ", "lim(", "sin(", "sen(", "cos(", "log(")
    return (
        any(term in text for term in question_terms)
        or any(marker in text for marker in math_markers)
        or len(text) >= 80
    )


def _session_is_due(
    session: dict[str, Any], *, now: float, inactivity_minutes: int
) -> bool:
    if session.get("prompt_message_id") is not None:
        return False
    try:
        last_activity = float(session["last_activity"])
    except (KeyError, TypeError, ValueError):
        return False
    return now - last_activity >= inactivity_minutes * 60


def _channel_setup_error(
    channel: discord.TextChannel,
    mentor_role: discord.Role | None = None,
) -> str | None:
    bot_member = channel.guild.me
    if bot_member is None:
        return "Não consegui localizar o meu próprio membro no servidor."

    permissions = channel.permissions_for(bot_member)
    required = {
        "ver o canal": permissions.view_channel,
        "enviar mensagens": permissions.send_messages,
        "ler o histórico": permissions.read_message_history,
        "adicionar reações": permissions.add_reactions,
    }
    missing = [name for name, allowed in required.items() if not allowed]
    if missing:
        return "Permissões ausentes para o bot: " + ", ".join(missing) + "."

    if (
        mentor_role is not None
        and not mentor_role.mentionable
        and not permissions.mention_everyone
    ):
        return (
            f"O cargo {mentor_role.mention} não é mencionável e o bot não possui "
            "a permissão de mencionar cargos neste canal."
        )
    return None


class AjudaMonitor(commands.Cog, name="AjudaMonitor"):
    """Monitora dúvidas e fornece menções controladas por slash."""

    marcar = app_commands.Group(
        name="marcar",
        description="Envia somente uma menção pelo bot.",
    )
    monitor_ajuda = app_commands.Group(
        name="monitor_ajuda",
        description="Configura a verificação automática de dúvidas.",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._guild_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._scheduler.start()

    def cog_unload(self) -> None:
        self._scheduler.cancel()

    async def _get_config(self, guild_id: int) -> dict[str, Any]:
        raw = await store.get(guild_id, NAMESPACE, CONFIG_KEY, {})
        return _normalize_config(raw)

    async def _save_config(self, guild_id: int, config: dict[str, Any]) -> None:
        await store.set(guild_id, NAMESPACE, CONFIG_KEY, _normalize_config(config))

    async def _get_sessions(self, guild_id: int) -> dict[str, dict[str, Any]]:
        raw = await store.get(guild_id, NAMESPACE, SESSIONS_KEY, {})
        return _normalize_sessions(raw)

    async def _save_sessions(
        self,
        guild_id: int,
        sessions: dict[str, dict[str, Any]],
    ) -> None:
        await store.set(guild_id, NAMESPACE, SESSIONS_KEY, sessions)

    @staticmethod
    async def _ephemeral(interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    @staticmethod
    def _bot_can_send(interaction: discord.Interaction) -> bool:
        channel = interaction.channel
        guild = interaction.guild
        if guild is None or channel is None or guild.me is None:
            return False
        permissions_for = getattr(channel, "permissions_for", None)
        if permissions_for is None:
            return False
        return bool(permissions_for(guild.me).send_messages)

    @marcar.command(
        name="membro", description="O bot envia apenas a menção de um membro."
    )
    @app_commands.describe(membro="Membro que será mencionado")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def marcar_membro(
        self,
        interaction: discord.Interaction,
        membro: discord.Member,
    ) -> None:
        if not self._bot_can_send(interaction):
            await self._ephemeral(
                interaction, "❌ Não tenho permissão para enviar mensagens aqui."
            )
            return
        await interaction.response.send_message(
            membro.mention,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=[membro],
                roles=False,
                replied_user=False,
            ),
        )

    @marcar.command(
        name="cargo", description="O bot envia apenas a menção de um cargo."
    )
    @app_commands.describe(cargo="Cargo que será mencionado")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def marcar_cargo(
        self,
        interaction: discord.Interaction,
        cargo: discord.Role,
    ) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or channel is None or guild.me is None:
            return

        user_permissions = interaction.permissions
        bot_permissions = channel.permissions_for(guild.me)
        if cargo.is_default():
            if not user_permissions.mention_everyone:
                await self._ephemeral(
                    interaction,
                    "❌ Você precisa da permissão **Mencionar @everyone** para usar esse cargo.",
                )
                return
            if not bot_permissions.mention_everyone:
                await self._ephemeral(
                    interaction,
                    "❌ O bot não possui a permissão **Mencionar @everyone** neste canal.",
                )
                return
            await interaction.response.send_message(
                "@everyone",
                allowed_mentions=discord.AllowedMentions(
                    everyone=True,
                    users=False,
                    roles=False,
                    replied_user=False,
                ),
            )
            return

        if not cargo.mentionable and not user_permissions.mention_everyone:
            await self._ephemeral(interaction, "❌ Você não pode mencionar esse cargo.")
            return
        if not cargo.mentionable and not bot_permissions.mention_everyone:
            await self._ephemeral(
                interaction, "❌ O bot não pode mencionar esse cargo neste canal."
            )
            return
        if not bot_permissions.send_messages:
            await self._ephemeral(
                interaction, "❌ Não tenho permissão para enviar mensagens aqui."
            )
            return

        await interaction.response.send_message(
            cargo.mention,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[cargo],
                replied_user=False,
            ),
        )

    @marcar.command(name="everyone", description="O bot envia apenas @everyone.")
    @app_commands.guild_only()
    @app_commands.default_permissions(mention_everyone=True)
    @app_commands.checks.has_permissions(mention_everyone=True)
    async def marcar_everyone(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or channel is None or guild.me is None:
            return
        permissions = channel.permissions_for(guild.me)
        if not permissions.send_messages or not permissions.mention_everyone:
            await self._ephemeral(
                interaction,
                "❌ Preciso das permissões de enviar mensagens e mencionar @everyone neste canal.",
            )
            return
        await interaction.response.send_message(
            "@everyone",
            allowed_mentions=discord.AllowedMentions(
                everyone=True,
                users=False,
                roles=False,
                replied_user=False,
            ),
        )

    @monitor_ajuda.command(
        name="configurar",
        description="Define o cargo Mentor e o tempo de inatividade.",
    )
    @app_commands.describe(
        cargo_mentor="Cargo chamado quando o autor informar que ainda precisa de ajuda",
        minutos="Minutos sem atividade antes da verificação (5 a 1440)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def configurar_monitor(
        self,
        interaction: discord.Interaction,
        cargo_mentor: discord.Role,
        minutos: app_commands.Range[
            int, MIN_INACTIVITY_MINUTES, MAX_INACTIVITY_MINUTES
        ] = (DEFAULT_INACTIVITY_MINUTES),
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if cargo_mentor.is_default():
            await self._ephemeral(
                interaction, "❌ Escolha um cargo específico, como **Mentor**."
            )
            return

        async with self._guild_locks[guild.id]:
            config = await self._get_config(guild.id)
            invalid_channels: list[str] = []
            for channel_id in config["channel_ids"]:
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    invalid_channels.append(f"`{channel_id}`")
                    continue
                if _channel_setup_error(channel, cargo_mentor):
                    invalid_channels.append(channel.mention)
            if invalid_channels:
                await self._ephemeral(
                    interaction,
                    "❌ Não consigo mencionar esse cargo nestes canais: "
                    + ", ".join(invalid_channels)
                    + ". Ajuste as permissões do bot e tente novamente.",
                )
                return

            config["mentor_role_id"] = cargo_mentor.id
            config["inactivity_minutes"] = int(minutos)
            config["enabled"] = bool(config["channel_ids"])
            await self._save_config(guild.id, config)

        state = (
            "ativado"
            if config["enabled"]
            else "configurado; adicione ao menos um canal"
        )
        await self._ephemeral(
            interaction,
            f"✅ Monitor {state}. Cargo: {cargo_mentor.mention} · Inatividade: **{minutos} min**.",
        )

    @monitor_ajuda.command(
        name="adicionar_canal",
        description="Adiciona um canal à lista de ajuda monitorada.",
    )
    @app_commands.describe(canal="Canal de ajuda que será acompanhado")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def adicionar_canal(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        async with self._guild_locks[guild.id]:
            config = await self._get_config(guild.id)
            role = (
                guild.get_role(config["mentor_role_id"])
                if config["mentor_role_id"]
                else None
            )
            if error := _channel_setup_error(canal, role):
                await self._ephemeral(interaction, f"❌ {error}")
                return
            if canal.id not in config["channel_ids"]:
                config["channel_ids"].append(canal.id)
                config["channel_ids"].sort()
            config["enabled"] = bool(config["mentor_role_id"] and config["channel_ids"])
            await self._save_config(guild.id, config)

        extra = "" if config["enabled"] else " Configure também o cargo Mentor."
        await self._ephemeral(interaction, f"✅ {canal.mention} foi adicionado.{extra}")

    @monitor_ajuda.command(
        name="adicionar_categoria",
        description="Adiciona todos os canais de texto de uma categoria.",
    )
    @app_commands.describe(categoria="Categoria que contém os canais de ajuda")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def adicionar_categoria(
        self,
        interaction: discord.Interaction,
        categoria: discord.CategoryChannel,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        added: list[discord.TextChannel] = []
        rejected: list[discord.TextChannel] = []
        async with self._guild_locks[guild.id]:
            config = await self._get_config(guild.id)
            role = (
                guild.get_role(config["mentor_role_id"])
                if config["mentor_role_id"]
                else None
            )
            for channel in categoria.text_channels:
                if _channel_setup_error(channel, role):
                    rejected.append(channel)
                    continue
                if channel.id not in config["channel_ids"]:
                    config["channel_ids"].append(channel.id)
                    added.append(channel)
            config["channel_ids"].sort()
            config["enabled"] = bool(config["mentor_role_id"] and config["channel_ids"])
            await self._save_config(guild.id, config)

        message = f"✅ **{len(added)}** canal(is) de {categoria.mention} adicionado(s)."
        if rejected:
            message += f" **{len(rejected)}** ignorado(s) por falta de permissões."
        if not config["enabled"]:
            message += " Configure também o cargo Mentor."
        await self._ephemeral(interaction, message)

    @monitor_ajuda.command(
        name="remover_canal",
        description="Remove um canal da lista monitorada.",
    )
    @app_commands.describe(canal="Canal que deixará de ser acompanhado")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def remover_canal(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        async with self._guild_locks[guild.id]:
            config = await self._get_config(guild.id)
            config["channel_ids"] = [
                channel_id
                for channel_id in config["channel_ids"]
                if channel_id != canal.id
            ]
            config["enabled"] = bool(config["mentor_role_id"] and config["channel_ids"])
            sessions = await self._get_sessions(guild.id)
            sessions = {
                key: session
                for key, session in sessions.items()
                if session["channel_id"] != canal.id
            }
            await self._save_config(guild.id, config)
            await self._save_sessions(guild.id, sessions)

        await self._ephemeral(
            interaction, f"✅ {canal.mention} foi removido do monitor."
        )

    @monitor_ajuda.command(
        name="status", description="Mostra a configuração atual do monitor."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def status_monitor(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        config = await self._get_config(guild.id)
        role = (
            guild.get_role(config["mentor_role_id"])
            if config["mentor_role_id"]
            else None
        )
        channels = [
            guild.get_channel(channel_id)
            for channel_id in config["channel_ids"]
            if guild.get_channel(channel_id) is not None
        ]
        channel_text = ", ".join(channel.mention for channel in channels) or "Nenhum"
        state = "Ativo" if config["enabled"] else "Desativado/incompleto"
        await self._ephemeral(
            interaction,
            f"**Estado:** {state}\n"
            f"**Cargo Mentor:** {role.mention if role else 'Não configurado'}\n"
            f"**Inatividade:** {config['inactivity_minutes']} min\n"
            f"**Canais:** {channel_text}",
        )

    @monitor_ajuda.command(
        name="desativar", description="Desativa o monitor e limpa sessões abertas."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def desativar_monitor(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        async with self._guild_locks[guild.id]:
            config = await self._get_config(guild.id)
            config["enabled"] = False
            await self._save_config(guild.id, config)
            await self._save_sessions(guild.id, {})
        await self._ephemeral(
            interaction, "✅ Monitor de ajuda desativado e sessões limpas."
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
            or message.webhook_id is not None
        ):
            return

        reservations = self.bot.get_cog("Ajuda Logos")
        if reservations and await reservations.manages(message.guild.id, message.channel.id):
            return
        config = await self._get_config(message.guild.id)
        if not config["enabled"] or message.channel.id not in config["channel_ids"]:
            return

        prefixes = await self.bot.get_prefix(message)
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if message.content.startswith(tuple(prefixes)):
            return

        async with self._guild_locks[message.guild.id]:
            sessions = await self._get_sessions(message.guild.id)
            owner_id = message.author.id
            referenced_message_id = None
            if message.reference is not None:
                referenced_message_id = message.reference.message_id
                resolved = message.reference.resolved
                if isinstance(resolved, discord.Message) and not resolved.author.bot:
                    owner_id = resolved.author.id

            session_key = _session_key(message.channel.id, owner_id)
            session = sessions.get(session_key)
            if session is None and referenced_message_id is not None:
                for existing_key, existing_session in sessions.items():
                    if (
                        existing_session["channel_id"] == message.channel.id
                        and existing_session["origin_message_id"]
                        == referenced_message_id
                    ):
                        session_key = existing_key
                        session = existing_session
                        break

            if session is None:
                # Respostas sem uma sessão correspondente normalmente são ajuda
                # prestada por outra pessoa, não uma nova dúvida do autor.
                if message.reference is not None:
                    return
                if not _looks_like_question(
                    message.content,
                    has_attachment=bool(message.attachments or message.stickers),
                ):
                    return
                session_key = _session_key(message.channel.id, message.author.id)
                sessions[session_key] = {
                    "channel_id": message.channel.id,
                    "owner_id": message.author.id,
                    "origin_message_id": message.id,
                    "last_activity": time.time(),
                    "prompt_message_id": None,
                    "mentor_pinged": False,
                }
            else:
                prompt_message_id = session.get("prompt_message_id")
                if prompt_message_id is not None:
                    try:
                        prompt = await message.channel.fetch_message(
                            int(prompt_message_id)
                        )
                        await prompt.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    session["prompt_message_id"] = None
                session["last_activity"] = time.time()
            await self._save_sessions(message.guild.id, sessions)

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if (
            payload.guild_id is None
            or self.bot.user is None
            or payload.user_id == self.bot.user.id
        ):
            return
        emoji = str(payload.emoji)
        if emoji not in {CHECK_MARK, CROSS_MARK}:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        reservations = self.bot.get_cog("Ajuda Logos")
        if reservations and await reservations.manages(guild.id, payload.channel_id):
            return
        config = await self._get_config(guild.id)
        if not config["enabled"] or payload.channel_id not in config["channel_ids"]:
            return

        async with self._guild_locks[guild.id]:
            sessions = await self._get_sessions(guild.id)
            session_key = None
            session = None
            for existing_key, existing_session in sessions.items():
                if (
                    existing_session["channel_id"] == payload.channel_id
                    and existing_session.get("prompt_message_id") == payload.message_id
                ):
                    session_key = existing_key
                    session = existing_session
                    break
            if session_key is None or session is None:
                return

            channel = guild.get_channel(payload.channel_id)
            if not isinstance(channel, discord.TextChannel):
                return

            if payload.user_id != session["owner_id"]:
                try:
                    partial = channel.get_partial_message(payload.message_id)
                    await partial.remove_reaction(
                        payload.emoji, discord.Object(payload.user_id)
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                return

            try:
                prompt = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                prompt = None

            if emoji == CHECK_MARK:
                sessions.pop(session_key, None)
                await self._save_sessions(guild.id, sessions)
                if prompt is not None:
                    try:
                        await prompt.edit(
                            content=f"✅ <@{payload.user_id}> informou que a dúvida foi resolvida.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        await prompt.clear_reactions()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                return

            session["prompt_message_id"] = None
            session["last_activity"] = time.time()
            mentor_role = guild.get_role(config["mentor_role_id"])
            if mentor_role is not None and not session.get("mentor_pinged", False):
                try:
                    reference = discord.MessageReference(
                        message_id=int(session["origin_message_id"]),
                        channel_id=channel.id,
                        guild_id=guild.id,
                        fail_if_not_exists=False,
                    )
                    await channel.send(
                        mentor_role.mention,
                        reference=reference,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False,
                            users=False,
                            roles=[mentor_role],
                            replied_user=False,
                        ),
                    )
                    session["mentor_pinged"] = True
                except (discord.Forbidden, discord.HTTPException):
                    log.exception(
                        "Falha ao mencionar o cargo Mentor no canal %s", channel.id
                    )

            await self._save_sessions(guild.id, sessions)
            if prompt is not None:
                try:
                    await prompt.edit(
                        content=f"❌ <@{payload.user_id}> informou que ainda precisa de ajuda.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await prompt.clear_reactions()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def _scheduler(self) -> None:
        now = time.time()
        for guild in self.bot.guilds:
            try:
                await self._process_guild(guild, now=now)
            except Exception:
                log.exception(
                    "Falha ao processar monitor de ajuda no servidor %s", guild.id
                )

    @_scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_guild(self, guild: discord.Guild, *, now: float) -> None:
        config = await self._get_config(guild.id)
        if not config["enabled"]:
            return

        async with self._guild_locks[guild.id]:
            sessions = await self._get_sessions(guild.id)
            changed = False
            for channel_key, session in list(sessions.items()):
                channel_id = int(session["channel_id"])
                reservations = self.bot.get_cog("Ajuda Logos")
                if reservations and await reservations.manages(guild.id, channel_id):
                    sessions.pop(channel_key, None)
                    changed = True
                    continue
                if channel_id not in config["channel_ids"]:
                    sessions.pop(channel_key, None)
                    changed = True
                    continue
                if not _session_is_due(
                    session,
                    now=now,
                    inactivity_minutes=config["inactivity_minutes"],
                ):
                    continue

                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    sessions.pop(channel_key, None)
                    changed = True
                    continue
                try:
                    prompt = await channel.send(
                        f"<@{session['owner_id']}> sua dúvida foi resolvida?",
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False,
                            users=[discord.Object(session["owner_id"])],
                            roles=False,
                            replied_user=False,
                        ),
                    )
                    await prompt.add_reaction(CHECK_MARK)
                    await prompt.add_reaction(CROSS_MARK)
                except (discord.Forbidden, discord.HTTPException):
                    log.exception(
                        "Falha ao publicar verificação no canal %s", channel.id
                    )
                    session["last_activity"] = now
                    changed = True
                    continue

                session["prompt_message_id"] = prompt.id
                changed = True

            if changed:
                await self._save_sessions(guild.id, sessions)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AjudaMonitor(bot))
