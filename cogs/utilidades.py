"""
Cog de Utilidades — comandos gerais, ajuda, ping e info.
"""

import discord
from discord.ext import commands
import time
import platform
import sys
import os
import logging

log = logging.getLogger('cogs.utilidades')


class Utilidades(commands.Cog, name='Utilidades'):
    """Comandos gerais de utilidade."""

    def __init__(self, bot):
        self.bot = bot
        self.start_time = time.time()

    @commands.command(name='ajuda', aliases=['help', 'h', 'comandos'])
    async def ajuda(self, ctx, *, comando: str = None):
        """Exibe a lista de comandos ou ajuda sobre um comando específico.

        Exemplos:
          .ajuda
          .ajuda calc
          .ajuda derivada
        """
        prefix = ctx.prefix

        if comando:
            cmd = self.bot.get_command(comando)
            if not cmd:
                return await ctx.send(f'❌ Comando `{comando}` não encontrado.')

            embed = discord.Embed(
                title=f'📖 Ajuda: {prefix}{cmd.name}',
                description=cmd.help or 'Sem descrição disponível.',
                color=0x5865F2
            )
            if cmd.aliases:
                embed.add_field(name='Aliases', value=', '.join([f'`{a}`' for a in cmd.aliases]), inline=False)
            embed.set_footer(text=f'Uso: {prefix}{cmd.name}')
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            title='📚 Bot do Logos — Comandos',
            description=f'Use `{prefix}ajuda <comando>` para mais detalhes.\nPrefixo atual: `{prefix}`',
            color=0x5865F2
        )

        # Organizar cogs
        cog_emojis = {
            'Matemática': '🔢',
            'Lembretes': '⏰',
            'Tags': '🏷️',
            'Utilidades': '🛠️',
        }

        for cog_name, cog in self.bot.cogs.items():
            cmds = [cmd for cmd in cog.get_commands() if not cmd.hidden]
            if not cmds:
                continue
            emoji = cog_emojis.get(cog_name, '📌')
            lista = ' '.join([f'`{prefix}{c.name}`' for c in cmds])
            embed.add_field(name=f'{emoji} {cog_name}', value=lista, inline=False)

        embed.add_field(
            name='✉️ Avisos privados — somente administradores',
            value=(
                '`/aviso enviar` · Escrever, revisar e enviar uma DM institucional\n'
                '`/aviso configurar_logs` · Definir o canal privado de auditoria\n'
                '`/aviso historico` · Consultar os últimos envios'
            ),
            inline=False,
        )

        embed.add_field(
            name='📣 Menções pelo bot — equipe',
            value=(
                '`/marcar membro` · Enviar somente a menção de um membro\n'
                '`/marcar cargo` · Enviar somente a menção de um cargo\n'
                '`/marcar everyone` · Enviar somente @everyone'
            ),
            inline=False,
        )

        embed.add_field(
            name='🧮 Monitor de dúvidas — somente administradores',
            value=(
                '`/monitor_ajuda configurar` · Definir cargo Mentor e tempo\n'
                '`/monitor_ajuda adicionar_canal` · Monitorar um canal de ajuda\n'
                '`/monitor_ajuda adicionar_categoria` · Monitorar toda uma categoria\n'
                '`/monitor_ajuda remover_canal` · Parar de monitorar um canal\n'
                '`/monitor_ajuda status` · Conferir a configuração\n'
                '`/monitor_ajuda desativar` · Desativar e limpar sessões'
            ),
            inline=False,
        )

        embed.add_field(
            name='📚 Salas públicas de ajuda — Logos',
            value=('`/logos_ajuda configurar` · Criar categorias e salas (admin)\n'
                   '`/logos_ajuda status` · Conferir reservas\n'
                   f'`{prefix}close` · Liberar sua sala\n'
                   f'`{prefix}chamar-ajuda` · Chamar ajudantes após 15 minutos\n'
                   '`/logos_ajuda pausar` · Pausar novas reservas (admin)'),
            inline=False,
        )

        embed.set_footer(text='Bot do Logos 🇧🇷')
        await ctx.send(embed=embed)

    @commands.command(name='ping')
    async def ping(self, ctx):
        """Mostra a latência do bot."""
        latencia = round(self.bot.latency * 1000)
        cor = 0x2ECC71 if latencia < 100 else (0xFEE75C if latencia < 200 else 0xED4245)
        embed = discord.Embed(title='🏓 Pong!', color=cor)
        embed.add_field(name='Latência', value=f'**{latencia}ms**')
        await ctx.send(embed=embed)

    @commands.command(name='info', aliases=['botinfo', 'sobre'])
    async def info(self, ctx):
        """Exibe informações sobre o bot."""
        uptime_segundos = int(time.time() - self.start_time)
        horas, resto = divmod(uptime_segundos, 3600)
        minutos, segundos = divmod(resto, 60)

        embed = discord.Embed(
            title='ℹ️ Sobre o Bot',
            description='Bot do Logos para servidores brasileiros. 🇧🇷',
            color=0x5865F2
        )
        embed.add_field(name='Prefixo', value=f'`{ctx.prefix}`', inline=True)
        embed.add_field(name='Latência', value=f'{round(self.bot.latency * 1000)}ms', inline=True)
        embed.add_field(name='Uptime', value=f'{horas}h {minutos}m {segundos}s', inline=True)
        embed.add_field(name='Servidores', value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name='Python', value=f'{sys.version_info.major}.{sys.version_info.minor}', inline=True)
        embed.add_field(name='discord.py', value=discord.__version__, inline=True)
        embed.set_footer(text='Feito com SymPy + discord.py')
        if self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name='prefixo', aliases=['prefix'])
    @commands.has_permissions(administrator=True)
    async def prefixo(self, ctx, novo_prefixo: str):
        """[Admin] Altera o prefixo do bot neste servidor.

        Exemplos:
          .prefixo !
          .prefixo >>
        """
        if len(novo_prefixo) > 5:
            return await ctx.send('❌ Prefixo muito longo (máx: 5 caracteres).')
        self.bot.command_prefix = novo_prefixo
        await ctx.send(f'✅ Prefixo alterado para `{novo_prefixo}`')

    @commands.command(name='unicode', aliases=['latex_info', 'simbolos'])
    async def unicode(self, ctx):
        """Mostra como escrever símbolos matemáticos nos comandos."""
        embed = discord.Embed(title='📝 Símbolos Matemáticos', color=0x9B59B6)
        embed.description = 'Use estes símbolos ao digitar expressões:'

        simbolos = [
            ('Potência', 'x**2 ou x^2', 'x²'),
            ('Raiz quadrada', 'sqrt(x)', '√x'),
            ('Pi', 'pi', 'π'),
            ('Euler (e)', 'E ou exp(1)', 'e ≈ 2,718'),
            ('Infinito', 'oo ou inf', '∞'),
            ('Seno', 'sin(x)', 'sen(x)'),
            ('Cosseno', 'cos(x)', 'cos(x)'),
            ('Tangente', 'tan(x)', 'tg(x)'),
            ('Logaritmo natural', 'log(x)', 'ln(x)'),
            ('Logaritmo base 10', 'log(x, 10)', 'log₁₀(x)'),
            ('Valor absoluto', 'Abs(x)', '|x|'),
            ('Número imaginário', 'I ou i', 'i = √(-1)'),
            ('Fatorial', 'factorial(n)', 'n!'),
        ]

        for nome, entrada, simbolo in simbolos:
            embed.add_field(name=nome, value=f'Digite: `{entrada}` → {simbolo}', inline=True)

        embed.set_footer(text='Exemplos: .calc sqrt(2) | .derivada sin(x)**2')
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Utilidades(bot))
