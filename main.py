"""Bot do Logos para Discord.

Carrega os recursos acadêmicos existentes, o sistema de níveis, as cores de
perfil e o construtor administrativo de painéis Components V2.
"""
from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

PREFIX = os.environ.get("BOT_PREFIX", ".")

COGS = [
    "cogs.utilidades",
    "cogs.lembretes",
    "cogs.tags",
    "cogs.roles_review",
    "cogs.cores",
    "cogs.painel_interativo",
    "cogs.tickets",
    "cogs.avisos_dm",
    "cogs.ajuda_monitor",
    "cogs.logos_help",
]


class LogosBot(commands.Bot):
    async def setup_hook(self) -> None:
        await store.init()
        for extension in COGS:
            try:
                await self.load_extension(extension)
                log.info("Cog carregado: %s", extension)
            except Exception:
                log.exception("Falha ao carregar o cog %s", extension)

        try:
            synced = await self.tree.sync()
            log.info("Comandos de aplicativo sincronizados: %s", len(synced))
        except discord.HTTPException:
            log.exception("Falha ao sincronizar os comandos de aplicativo")

    async def close(self) -> None:
        await store.close()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = LogosBot(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready() -> None:
    if bot.user is None:
        return
    log.info("Bot conectado como %s (ID: %s)", bot.user, bot.user.id)
    log.info("Prefixo: %s", PREFIX)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{PREFIX}ajuda | Up",
        )
    )


async def _safe_command_error(ctx, text):
    try:
        await ctx.send(text)
    except discord.HTTPException:
        log.warning("Não foi possível responder ao erro: canal removido ou inacessível.")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await _safe_command_error(ctx,
            f"Argumento faltando: `{error.param.name}`. "
            f"Use `{PREFIX}ajuda {ctx.command}` para consultar o uso."
        )
    elif isinstance(error, commands.BadArgument):
        await _safe_command_error(ctx, f"Argumento inválido. Use `{PREFIX}ajuda {ctx.command}` para consultar o uso.")
    elif isinstance(error, commands.CommandOnCooldown):
        await _safe_command_error(ctx, f"Aguarde {error.retry_after:.1f}s antes de usar este comando novamente.")
    elif isinstance(error, commands.MissingPermissions):
        await _safe_command_error(ctx, "Você não tem permissão para usar este comando.")
    else:
        log.error("Erro no comando %s", ctx.command, exc_info=error)
        await _safe_command_error(ctx, "Ocorreu um erro inesperado ao executar o comando.")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "Você não tem permissão para usar este comando."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "Este comando só pode ser usado dentro de um servidor."
    else:
        log.error("Erro em comando de aplicativo", exc_info=error)
        message = "Ocorreu um erro ao executar este comando."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.critical("DISCORD_TOKEN não encontrado nas variáveis de ambiente")
        return
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
