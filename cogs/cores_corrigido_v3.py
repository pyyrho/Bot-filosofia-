"""Sistema persistente de cores de perfil usando Discord Components V2.

Adaptado para o Bot-Math a partir do painel de cores fornecido pelo usuário.
Mantém os emojis personalizados nos botões e permite apenas uma cor por membro.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from functools import wraps
import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import store, StorageUnavailable
from utils.ui_v2 import DEFAULT_ACCENT, parse_emoji, parse_hex, status_layout, valid_url

log = logging.getLogger("cogs.cores")


async def _defer(interaction):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


async def _respond(interaction, *args, **kwargs):
    if interaction.response.is_done():
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)


def _timely(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        interaction = kwargs.get('interaction')
        if interaction is None:
            interaction = next(arg for arg in args if hasattr(arg, 'response') and hasattr(arg, 'followup'))
        try:
            await _defer(interaction)
            return await function(*args, **kwargs)
        except StorageUnavailable:
            await _respond(interaction, 'O banco está temporariamente indisponível. A operação não foi confirmada; tente novamente em instantes.', ephemeral=True)
        except discord.NotFound as exc:
            if exc.code not in {10003, 10062, 10008}:
                raise
            log.warning('Interação/canal não existe mais; operação encerrada sem repetir chamadas.')
    return wrapped


NAMESPACE = "cores"

CORES_NORMAIS = [
    ("vermelho", "Vermelho", "<:1000010231:1482084219814416464>", 0xE74C3C),
    ("laranja", "Laranja", "<:1000010232:1482092477946134579>", 0xE67E22),
    ("amarelo", "Amarelo", "<:1000010233:1482092507755188244>", 0xF1C40F),
    ("verde", "Verde", "<:1000010234:1482092539195424768>", 0x2ECC71),
    ("azul", "Azul", "<:1000010235:1482092570023825591>", 0x3498DB),
    ("rosa", "Rosa", "<:1000010236:1482092600172351589>", 0xFF69B4),
    ("marrom", "Marrom", "<:1000010263:1482103964215279706>", 0x8B4513),
    ("branco", "Branco", "<:1000010273:1482103994913525892>", 0xFFFFFF),
    ("roxo", "Roxo", "<:1000018289:1492560763682951178>", 0x8A2BE2),
]

CORES_DEGRADE = [
    ("grad_1", "Degradê 1", "<:1000010250:1482092724428603412>", 0x9B59B6),
    ("grad_2", "Degradê 2", "<:1000010264:1482104044699779173>", 0x9B59B6),
    ("grad_3", "Degradê 3", "<:1000010265:1482104072898347028>", 0x9B59B6),
    ("grad_4", "Degradê 4", "<:1000010266:1482104100320710778>", 0x9B59B6),
    ("grad_5", "Degradê 5", "<:1000010267:1482104126753079498>", 0x9B59B6),
    ("grad_6", "Degradê 6", "<:1000010268:1482104151751004421>", 0x9B59B6),
    ("grad_7", "Degradê 7", "<:1000010269:1482104177302966342>", 0x9B59B6),
    ("grad_8", "Degradê 8", "<:1000010270:1482104213315260466>", 0x9B59B6),
]

ALL_COLORS = CORES_NORMAIS + CORES_DEGRADE
COLOR_BY_KEY = {key: (label, emoji, accent) for key, label, emoji, accent in ALL_COLORS}


def _manageable(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(
        me
        and me.guild_permissions.manage_roles
        and role < me.top_role
        and not role.managed
        and role != guild.default_role
    )


async def _get_config(guild_id: int) -> dict[str, Any]:
    value = await store.get(guild_id, NAMESPACE, "config", None)
    if not isinstance(value, dict):
        value = {}
    value.setdefault("roles", {})
    value.setdefault("vip_channel", 0)
    value.setdefault(
        "normal_panel",
        {
            "title": "Cores de perfil",
            "description": (
                "Escolha uma cor para o seu apelido. Você pode manter somente uma cor por vez.\n\n"
                "Clique novamente na cor selecionada para removê-la."
            ),
            "accent": DEFAULT_ACCENT,
            "thumbnail": None,
            "banner": None,
        },
    )
    value.setdefault(
        "gradient_panel",
        {
            "title": "Cores degradê",
            "description": (
                "Escolha um degradê exclusivo para o seu apelido. "
                "O acesso depende da área VIP configurada pela administração."
            ),
            "accent": 0x9B59B6,
            "thumbnail": None,
            "banner": None,
        },
    )
    return value


async def _save_config(guild_id: int, config: dict[str, Any]) -> None:
    await store.set(guild_id, NAMESPACE, "config", config)


class ColorButton(discord.ui.Button):
    def __init__(self, key: str) -> None:
        label, emoji, _ = COLOR_BY_KEY[key]
        super().__init__(
            label=None,
            emoji=parse_emoji(emoji),
            style=discord.ButtonStyle.secondary,
            custom_id=f"profilecolor:{key}",
        )
        self.color_key = key
        self.color_label = label

    @_timely
    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Cores")
        if not isinstance(cog, Cores):
            await _respond(interaction, "O sistema de cores está indisponível.", ephemeral=True)
            return
        await cog.toggle_color(interaction, self.color_key)


class ColorPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        gradient: bool,
        title: str,
        description: str,
        accent: int,
        thumbnail: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=None)
        source = CORES_DEGRADE if gradient else CORES_NORMAIS
        content = f"# {title}\n{description}".strip()
        children: list[discord.ui.Item] = []

        if thumbnail:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(content),
                    accessory=discord.ui.Thumbnail(thumbnail),
                )
            )
        else:
            children.append(discord.ui.TextDisplay(content))

        if banner:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=banner)
            children.append(gallery)

        buttons = [ColorButton(key) for key, *_ in source]
        for index in range(0, len(buttons), 3):
            children.append(discord.ui.ActionRow(*buttons[index : index + 3]))

        children.append(discord.ui.TextDisplay("-# Nick Color: apenas uma cor por vez"))
        self.add_item(discord.ui.Container(*children, accent_color=accent))


class Cores(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Views-modelo para que os botões antigos continuem funcionando após reinícios.
        self.bot.add_view(
            ColorPanelView(
                gradient=False,
                title="Cores de perfil",
                description="Escolha uma cor para o seu apelido.",
                accent=DEFAULT_ACCENT,
            )
        )
        self.bot.add_view(
            ColorPanelView(
                gradient=True,
                title="Cores degradê",
                description="Escolha uma cor degradê.",
                accent=0x9B59B6,
            )
        )

    @_timely
    async def toggle_color(self, interaction: discord.Interaction, key: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await _respond(interaction, "Este botão só funciona em servidores.", ephemeral=True)
            return
        if key not in COLOR_BY_KEY:
            await _respond(interaction, "Cor inválida.", ephemeral=True)
            return

        config = await _get_config(interaction.guild.id)
        role_id = config.get("roles", {}).get(key, 0)
        try:
            role = interaction.guild.get_role(int(role_id)) if role_id else None
        except (TypeError, ValueError):
            role = None

        if not role:
            await _respond(interaction,
                view=status_layout("Cor não configurada", "A administração ainda não vinculou um cargo a esta cor."),
                ephemeral=True,
            )
            return
        if not _manageable(interaction.guild, role):
            await _respond(interaction,
                view=status_layout(
                    "Cargo inalcançável",
                    "O cargo do bot precisa ficar acima do cargo de cor.",
                ),
                ephemeral=True,
            )
            return

        if key.startswith("grad_"):
            vip_channel_id = config.get("vip_channel", 0)
            try:
                vip_channel = interaction.guild.get_channel(int(vip_channel_id)) if vip_channel_id else None
            except (TypeError, ValueError):
                vip_channel = None
            if isinstance(vip_channel, discord.abc.GuildChannel):
                if not vip_channel.permissions_for(interaction.user).view_channel:
                    await _respond(interaction,
                        view=status_layout(
                            "Cor exclusiva",
                            "Você não possui acesso às cores degradê deste servidor.",
                            accent=0x9B59B6,
                        ),
                        ephemeral=True,
                    )
                    return

        await _defer(interaction)
        configured_ids: set[int] = set()
        for configured_id in config.get("roles", {}).values():
            try:
                configured_ids.add(int(configured_id))
            except (TypeError, ValueError):
                continue

        other_roles = [
            member_role
            for member_role in interaction.user.roles
            if member_role.id in configured_ids and member_role.id != role.id and _manageable(interaction.guild, member_role)
        ]

        try:
            if other_roles:
                await interaction.user.remove_roles(*other_roles, reason="Troca de cor de perfil")
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Remoção voluntária de cor")
                title = "Cor removida"
                description = f"A cor **{role.name}** foi removida do seu perfil."
            else:
                await interaction.user.add_roles(role, reason="Seleção voluntária de cor")
                title = "Cor aplicada"
                description = f"A cor **{role.name}** foi aplicada ao seu perfil."
        except discord.HTTPException:
            log.exception("Falha ao alterar cor %s de %s", role.id, interaction.user.id)
            await interaction.followup.send(
                view=status_layout("Falha ao alterar", "Verifique a hierarquia de cargos do bot."),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            view=status_layout(title, description, accent=role.color.value or DEFAULT_ACCENT),
            ephemeral=True,
        )

    cores_group = app_commands.Group(
        name="cores",
        description="Sistema de cores de perfil",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @cores_group.command(name="vincular_normal", description="Vincula uma cor normal a um cargo")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(
        cor=[app_commands.Choice(name=label, value=key) for key, label, *_ in CORES_NORMAIS]
    )
    @_timely
    async def bind_normal(
        self,
        interaction: discord.Interaction,
        cor: app_commands.Choice[str],
        cargo: discord.Role,
    ) -> None:
        assert interaction.guild is not None
        if not _manageable(interaction.guild, cargo):
            await _respond(interaction,
                view=status_layout("Cargo inalcançável", "Coloque o cargo do bot acima deste cargo."),
                ephemeral=True,
            )
            return
        config = await _get_config(interaction.guild.id)
        config["roles"][cor.value] = cargo.id
        await _save_config(interaction.guild.id, config)
        await _respond(interaction,
            view=status_layout("Cor vinculada", f"**{cor.name}** foi vinculada a {cargo.mention}."),
            ephemeral=True,
        )

    @cores_group.command(name="vincular_degrade", description="Vincula uma cor degradê a um cargo")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(
        cor=[app_commands.Choice(name=label, value=key) for key, label, *_ in CORES_DEGRADE]
    )
    @_timely
    async def bind_gradient(
        self,
        interaction: discord.Interaction,
        cor: app_commands.Choice[str],
        cargo: discord.Role,
    ) -> None:
        assert interaction.guild is not None
        if not _manageable(interaction.guild, cargo):
            await _respond(interaction,
                view=status_layout("Cargo inalcançável", "Coloque o cargo do bot acima deste cargo."),
                ephemeral=True,
            )
            return
        config = await _get_config(interaction.guild.id)
        config["roles"][cor.value] = cargo.id
        await _save_config(interaction.guild.id, config)
        await _respond(interaction,
            view=status_layout(
                "Degradê vinculado",
                f"**{cor.name}** foi vinculado a {cargo.mention}. Configure o gradiente no próprio cargo do Discord.",
                accent=0x9B59B6,
            ),
            ephemeral=True,
        )

    @cores_group.command(name="canal_vip", description="Define o canal usado para validar acesso aos degradês")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @_timely
    async def configure_vip(self, interaction: discord.Interaction, canal: discord.TextChannel) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id)
        config["vip_channel"] = canal.id
        await _save_config(interaction.guild.id, config)
        await _respond(interaction,
            view=status_layout(
                "Acesso VIP configurado",
                f"Somente membros que conseguem visualizar {canal.mention} poderão usar degradês.",
                accent=0x9B59B6,
            ),
            ephemeral=True,
        )

    @_timely
    async def _publish_panel(
        self,
        interaction: discord.Interaction,
        *,
        gradient: bool,
        canal: Optional[discord.TextChannel],
        titulo: Optional[str],
        descricao: Optional[str],
        cor: Optional[str],
        miniatura: Optional[str],
        banner: Optional[str],
    ) -> None:
        assert interaction.guild is not None
        channel = canal or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await _respond(interaction, "Escolha um canal de texto.", ephemeral=True)
            return
        if not valid_url(miniatura) or not valid_url(banner):
            await _respond(interaction, "Miniatura e banner precisam usar uma URL http/https.", ephemeral=True)
            return

        config = await _get_config(interaction.guild.id)
        panel_key = "gradient_panel" if gradient else "normal_panel"
        panel = config[panel_key]
        try:
            accent = parse_hex(cor, int(panel.get("accent", DEFAULT_ACCENT)))
        except ValueError as exc:
            await _respond(interaction, str(exc), ephemeral=True)
            return

        if titulo is not None:
            panel["title"] = titulo[:200]
        if descricao is not None:
            panel["description"] = descricao[:3500]
        if cor is not None:
            panel["accent"] = accent
        if miniatura is not None:
            panel["thumbnail"] = miniatura.strip() or None
        if banner is not None:
            panel["banner"] = banner.strip() or None
        await _save_config(interaction.guild.id, config)

        view = ColorPanelView(
            gradient=gradient,
            title=panel["title"],
            description=panel["description"],
            accent=int(panel.get("accent", accent)),
            thumbnail=panel.get("thumbnail"),
            banner=panel.get("banner"),
        )
        try:
            await channel.send(view=view)
        except discord.HTTPException:
            await _respond(interaction,
                view=status_layout("Falha ao publicar", "Verifique as permissões do bot nesse canal."),
                ephemeral=True,
            )
            return
        await _respond(interaction,
            view=status_layout("Painel publicado", f"O painel foi enviado para {channel.mention}."),
            ephemeral=True,
        )

    @cores_group.command(name="painel", description="Publica o painel de cores normais")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @_timely
    async def normal_panel(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.TextChannel] = None,
        titulo: Optional[str] = None,
        descricao: Optional[str] = None,
        cor: Optional[str] = None,
        miniatura: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> None:
        await self._publish_panel(
            interaction,
            gradient=False,
            canal=canal,
            titulo=titulo,
            descricao=descricao,
            cor=cor,
            miniatura=miniatura,
            banner=banner,
        )

    @cores_group.command(name="painel_vip", description="Publica o painel de cores degradê")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @_timely
    async def gradient_panel(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.TextChannel] = None,
        titulo: Optional[str] = None,
        descricao: Optional[str] = None,
        cor: Optional[str] = None,
        miniatura: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> None:
        await self._publish_panel(
            interaction,
            gradient=True,
            canal=canal,
            titulo=titulo,
            descricao=descricao,
            cor=cor,
            miniatura=miniatura,
            banner=banner,
        )

    @cores_group.command(name="lista", description="Mostra todos os cargos de cor configurados")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @_timely
    async def list_colors(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id)
        normal_lines: list[str] = []
        gradient_lines: list[str] = []
        for key, label, emoji, _ in CORES_NORMAIS:
            role_id = config.get("roles", {}).get(key, 0)
            try:
                role = interaction.guild.get_role(int(role_id)) if role_id else None
            except (TypeError, ValueError):
                role = None
            normal_lines.append(f"{emoji} **{label}:** {role.mention if role else '`não configurado`'}")
        for key, label, emoji, _ in CORES_DEGRADE:
            role_id = config.get("roles", {}).get(key, 0)
            try:
                role = interaction.guild.get_role(int(role_id)) if role_id else None
            except (TypeError, ValueError):
                role = None
            gradient_lines.append(f"{emoji} **{label}:** {role.mention if role else '`não configurado`'}")

        vip_id = config.get("vip_channel", 0)
        try:
            vip = interaction.guild.get_channel(int(vip_id)) if vip_id else None
        except (TypeError, ValueError):
            vip = None
        description = (
            "## Cores normais\n"
            + "\n".join(normal_lines)
            + "\n\n## Cores degradê\n"
            + "\n".join(gradient_lines)
            + f"\n\n**Canal VIP:** {vip.mention if vip else '`não configurado`'}"
        )
        await _respond(interaction,
            view=status_layout("Configuração das cores", description),
            ephemeral=True,
        )

    @cores_group.command(name="remover", description="Remove o cargo de cor de um membro")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @_timely
    async def remove_color(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id)
        role_ids: set[int] = set()
        for value in config.get("roles", {}).values():
            try:
                role_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        roles = [
            role
            for role in membro.roles
            if role.id in role_ids and _manageable(interaction.guild, role)
        ]
        await _defer(interaction)
        try:
            if roles:
                await membro.remove_roles(*roles, reason=f"Cor removida por {interaction.user}")
        except discord.HTTPException:
            await interaction.followup.send(
                view=status_layout("Falha ao remover", "Verifique a hierarquia dos cargos."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            view=status_layout(
                "Cor removida",
                f"Foram removidos `{len(roles)}` cargo(s) de cor de {membro.mention}.",
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Cores(bot))
