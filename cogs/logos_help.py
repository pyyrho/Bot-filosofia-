"""Salas públicas de ajuda do Logos, inspiradas no fluxo clopen de discord-math/bot.

Implementação independente para discord.py e para o KVStore deste repositório.
O estado é persistido antes de mover canais e reconciliado após reinicialização.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from utils.storage import store, StorageUnavailable

log = logging.getLogger(__name__)
NAMESPACE = 'logos_help'
STATE_KEY = 'state'
NONE = discord.AllowedMentions.none()


def default_state() -> dict[str, Any]:
    return {'enabled': False, 'available_category_id': None, 'occupied_category_id': None,
            'guide_channel_id': None, 'helper_role_id': None, 'inactivity_minutes': 30,
            'ping_wait_minutes': 15, 'channels': {}}


def owner_channel(state: dict, owner_id: int) -> int | None:
    for key, room in state['channels'].items():
        if room.get('owner_id') == owner_id:
            return int(key)
    return None


def room_name(room: dict, member: discord.Member | None) -> str:
    base = room['base_name']
    if not room.get('owner_id'):
        return base
    name = member.display_name if member else str(room['owner_id'])
    name = re.sub(r'[^\w\-]', '-', name, flags=re.UNICODE).strip('-').lower()[:45] or 'membro'
    return f'{base}｜{name}'[:100]


def instructions(prefix: str, inactivity: int) -> str:
    return (
        '**Como pedir ajuda no Logos**\n\n'
        'Escolha um canal `help-N` em **Disponíveis** e publique sua dúvida diretamente. '
        'O bot reserva a sala para você e a move para **Ocupados**. Outras pessoas podem entrar e ajudar.\n\n'
        'Explique os detalhes relevantes, o que você tentou e onde ficou travado. '
        'Não precisa perguntar se pode perguntar. A ideia é aprender, não receber apenas uma resposta pronta.\n\n'
        'Use uma sala por vez e não repita a dúvida em outros canais. '
        'Mantenha uma dúvida principal por reserva; encerre a sala antes de abrir outra.\n\n'
        f'Quando terminar, use `{prefix}close` ou `/logos_ajuda fechar`. '
        f'A sala também é liberada depois de **{inactivity} minutos sem mensagens humanas**. '
        'O histórico não é apagado; cada reserva é separada por uma mensagem do bot.\n\n'
        f'Se continuar sem a ajuda de que precisa após **15 minutos**, use `{prefix}chamar-ajuda` '
        'ou `/logos_ajuda chamar`, **uma vez por reserva**, se houver um cargo de ajudantes configurado. '
        'Não mencione ou envie DMs individuais, nem chame moderadores para resolver sua dúvida.\n\n'
        'As salas atendem às diferentes áreas do **Logos**. '
        'Respeite as regras do servidor e ajude com explicações. '
        'Cola em provas, exames e outras formas de desonestidade acadêmica não são permitidas '
        'e ficam sujeitas à moderação do Logos.'
    )


class LogosHelp(commands.Cog, name='Ajuda Logos'):
    logos_ajuda = app_commands.Group(name='logos_ajuda', description='Salas públicas de ajuda do Logos.')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._rename_tasks: dict[int, asyncio.Task] = {}
        self._rename_after: dict[int, float] = {}
        self._desired_names: dict[int, tuple[discord.TextChannel, str]] = {}
        self._recovered_channels: set[int] = set()
        self._activity: dict[int, float] = {}
        self._applied_names: dict[int, str] = {}
        self._applied_categories: dict[int, int] = {}
        self.scheduler.start()

    def cog_unload(self):
        self.scheduler.cancel()
        for task in self._rename_tasks.values():
            task.cancel()

    async def state(self, guild_id: int) -> dict:
        raw = await store.get(guild_id, NAMESPACE, STATE_KEY, {})
        result = default_state()
        if isinstance(raw, dict):
            result.update(raw)
        if not isinstance(result.get('channels'), dict):
            result['channels'] = {}
        return result

    async def save(self, guild_id: int, state: dict):
        await store.set(guild_id, NAMESPACE, STATE_KEY, state)

    async def manages(self, guild_id: int, channel_id: int) -> bool:
        # Mesmo desativadas, estas salas pertencem ao fluxo de reservas.
        return str(channel_id) in (await self.state(guild_id))['channels']

    async def prefix(self, guild: discord.Guild) -> str:
        value = self.bot.command_prefix
        return value if isinstance(value, str) else '.'

    @staticmethod
    def is_staff(member: discord.Member) -> bool:
        return member.guild_permissions.manage_channels

    @staticmethod
    async def say(channel, text: str):
        try:
            return await asyncio.wait_for(channel.send(text, allowed_mentions=NONE), timeout=12)
        except discord.NotFound:
            return None

    def rename_later(self, channel: discord.TextChannel, name: str):
        if (channel.name == name or self._applied_names.get(channel.id) == name) and channel.id not in self._desired_names:
            return
        self._desired_names[channel.id] = (channel, name)
        if channel.id not in self._rename_tasks:
            # Um reinício não zera o limite do Discord. Edições cosméticas
            # ficam fora do caminho de reserva/fechamento.
            self._rename_after.setdefault(channel.id, time.monotonic() + 610)
            self._rename_tasks[channel.id] = asyncio.create_task(self._rename(channel.id))

    async def _rename(self, channel_id: int):
        try:
            while channel_id in self._desired_names:
                delay = self._rename_after.get(channel_id, 0) - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                channel, name = self._desired_names.pop(channel_id)
                if channel.name != name and self._applied_names.get(channel.id) != name:
                    try:
                        updated = await asyncio.wait_for(channel.edit(name=name, reason='Nome da reserva Logos'), timeout=12)
                        self._applied_names[channel_id] = updated.name if isinstance(updated, discord.TextChannel) else name
                        self._rename_after[channel_id] = time.monotonic() + 610
                    except (discord.HTTPException, asyncio.TimeoutError):
                        self._rename_after[channel_id] = time.monotonic() + 610
                        log.warning('Nome da sala Logos %s será tentado depois do intervalo de segurança.', channel_id)
                        break
        finally:
            self._rename_tasks.pop(channel_id, None)

    async def enact(self, guild: discord.Guild, state: dict, channel: discord.TextChannel, room: dict):
        occupied = bool(room.get('owner_id'))
        category_id = state['occupied_category_id'] if occupied else state['available_category_id']
        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            raise RuntimeError('Uma categoria de ajuda foi excluída. Execute /logos_ajuda configurar.')
        # Nome e tópico compartilham o limite lento de edições de canais.
        # Mover usa o endpoint de posições; não altera tópico a cada reserva.
        # Guardamos a projeção aplicada porque edit/move não atualizam o objeto
        # recebido imediatamente; o Gateway pode chegar depois da resposta HTTP.
        effective_category = self._applied_categories.get(channel.id, channel.category_id)
        if effective_category != category.id:
            await asyncio.wait_for(channel.move(category=category, sync_permissions=False,
                                   end=True, reason='Movimentação de reserva Logos'), timeout=12)
            self._applied_categories[channel.id] = category.id
        self.rename_later(channel, room_name(room, guild.get_member(room.get('owner_id', 0))))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or getattr(message, 'webhook_id', None) or not isinstance(message.channel, discord.TextChannel):
            return
        async with self.locks[message.guild.id]:
            state = await self.state(message.guild.id)
            room = state['channels'].get(str(message.channel.id))
            if not state['enabled'] or room is None:
                return
            prefixes = await self.bot.get_prefix(message)
            prefixes = [prefixes] if isinstance(prefixes, str) else prefixes
            # Comandos e mensagens sem texto/anexos não reservam uma sala.
            if any(message.content.startswith(p) for p in prefixes if p):
                return
            if not message.content.strip() and not message.attachments:
                return
            if room.get('closing'):
                await self.say(message.channel, 'Esta sala está sendo liberada; aguarde a confirmação antes de publicar.')
                return
            if room.get('owner_id'):
                self._recovered_channels.add(message.channel.id)
                # Atividade frequente não precisa gravar o mesmo JSON inteiro
                # em PostgreSQL para cada mensagem. O scheduler persiste em lote.
                self._activity[message.channel.id] = time.time()
                return
            existing = owner_channel(state, message.author.id)
            if existing:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                await self.say(message.channel, f'<@{message.author.id}>, você já tem uma reserva em <#{existing}>. Continue lá e não duplique sua pergunta.')
                return
            now = time.time()
            self._recovered_channels.add(message.channel.id)
            room.update(owner_id=message.author.id, origin_message_id=message.id,
                        opened_at=now, last_activity=now, pinged=False, closing=False)
            await self.save(message.guild.id, state)
            try:
                await self.enact(message.guild, state, message.channel, room)
            except (discord.HTTPException, RuntimeError, asyncio.TimeoutError):
                # A reserva persistida impede outra pessoa de assumir a mesma sala.
                # O scheduler repetirá a aplicação quando as permissões voltarem.
                log.exception('Reserva Logos salva, mas a movimentação falhou')
                await self.say(message.channel, 'A reserva foi registrada, mas não consegui mover a sala. A equipe deve conferir minhas permissões e categorias.')
                return
            prefix = await self.prefix(message.guild)
            await self.say(message.channel, f'**Sala reservada para <@{message.author.id}>.**\n'
                           f'Outros membros podem ajudar nesta dúvida. Ao terminar, use `{prefix}close`. '
                           f'Liberação automática após {state["inactivity_minutes"]} minutos sem atividade.')

    async def release(self, guild: discord.Guild, state: dict, channel: discord.TextChannel, room: dict, reason: str):
        # Fechamento pendente sobrevive a falhas de API/reinícios.
        room['closing'] = True
        room['close_reason'] = reason
        await self.save(guild.id, state)
        await self.finish_release(guild, state, channel, room)

    async def finish_release(self, guild: discord.Guild, state: dict, channel: discord.TextChannel, room: dict):
        available = {'base_name': room['base_name'], 'owner_id': None}
        await self.enact(guild, state, channel, available)
        reason = room.get('close_reason', 'Encerrada')
        await self.say(channel, f'✅ **Sala disponível — {reason}.**\nPublique uma nova dúvida para reservar este canal. O atendimento anterior está acima.')
        state['channels'][str(channel.id)] = available
        self._activity.pop(channel.id, None)
        await self.save(guild.id, state)

    async def close_room(self, guild, channel, actor) -> str:
        async with self.locks[guild.id]:
            state = await self.state(guild.id)
            room = state['channels'].get(str(channel.id))
            if not room:
                return 'Este não é um canal de ajuda do Logos.'
            if not room.get('owner_id'):
                return 'Esta sala já está disponível.'
            if room['owner_id'] != actor.id and not self.is_staff(actor):
                return 'Somente quem reservou a sala ou a equipe com Gerenciar canais pode encerrá-la.'
            try:
                await self.release(guild, state, channel, room, 'encerrada por quem reservou' if actor.id == room['owner_id'] else 'encerrada pela equipe')
            except StorageUnavailable:
                raise
            except (discord.HTTPException, RuntimeError, asyncio.TimeoutError):
                log.exception('Fechamento Logos pendente')
                return 'O fechamento foi registrado, mas a liberação está pendente. Confira as permissões e categorias; o bot tentará novamente.'
            return 'Sala encerrada e liberada.'

    async def ping_helpers(self, guild, channel, actor) -> str:
        async with self.locks[guild.id]:
            state = await self.state(guild.id)
            room = state['channels'].get(str(channel.id))
            if not state['enabled'] or not room or not room.get('owner_id') or room.get('closing'):
                return 'Use este comando na sua sala ocupada do Logos.'
            if room['owner_id'] != actor.id:
                return 'Somente quem reservou a sala pode chamar os ajudantes.'
            if room.get('pinged'):
                return 'Os ajudantes já foram chamados nesta reserva.'
            seconds = int(state['ping_wait_minutes'] * 60 - (time.time() - room['opened_at']))
            if seconds > 0:
                return f'Aguarde mais {(seconds + 59) // 60} minuto(s) antes de chamar os ajudantes.'
            role = guild.get_role(state.get('helper_role_id'))
            if role is None or role.is_default():
                return 'A equipe ainda não configurou um cargo de ajudantes válido.'
            perms = channel.permissions_for(guild.me)
            if not role.mentionable and not perms.mention_everyone:
                return 'O cargo não é mencionável e o bot não pode mencionar cargos aqui. A equipe precisa ajustar isso.'
            # Marcar antes do envio garante no máximo uma tentativa mesmo se
            # houver reinício depois de o Discord aceitar a menção.
            room['pinged'] = True
            await self.save(guild.id, state)
            try:
                await channel.send(f'{role.mention} — <@{actor.id}> ainda precisa de ajuda nesta dúvida.',
                                   allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role], replied_user=False))
            except discord.HTTPException:
                room['pinged'] = False
                await self.save(guild.id, state)
                raise
            return 'Ajudantes chamados. Não repita a menção.'

    @commands.command(name='close', aliases=['solved', 'fechar-ajuda'])
    @commands.guild_only()
    async def close_command(self, ctx: commands.Context):
        """Libera sua sala de ajuda do Logos; a equipe também pode encerrar."""
        try:
            result = await self.close_room(ctx.guild, ctx.channel, ctx.author)
        except StorageUnavailable:
            result = 'O banco está temporariamente indisponível. A liberação não foi confirmada; tente novamente em instantes.'
        await self.say(ctx.channel, result)

    @commands.command(name='chamar-ajuda')
    @commands.guild_only()
    async def call_command(self, ctx: commands.Context):
        """Chama os ajudantes uma vez por reserva, após 15 minutos."""
        try:
            result = await self.ping_helpers(ctx.guild, ctx.channel, ctx.author)
        except StorageUnavailable:
            result = 'O banco está temporariamente indisponível. Tente novamente em instantes.'
        await self.say(ctx.channel, result)

    @logos_ajuda.command(name='fechar', description='Encerra e libera sua sala de ajuda.')
    @app_commands.guild_only()
    async def close_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.close_room(interaction.guild, interaction.channel, interaction.user)
        except StorageUnavailable:
            result = 'O banco está temporariamente indisponível. A liberação não foi confirmada; tente novamente.'
        await interaction.followup.send(result, ephemeral=True)

    @logos_ajuda.command(name='chamar', description='Chama os ajudantes uma vez após 15 minutos de reserva.')
    @app_commands.guild_only()
    async def call_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.ping_helpers(interaction.guild, interaction.channel, interaction.user)
        except StorageUnavailable:
            result = 'O banco está temporariamente indisponível. Tente novamente em instantes.'
        await interaction.followup.send(result, ephemeral=True)

    @logos_ajuda.command(name='configurar', description='Cria ou completa as categorias e salas públicas de ajuda do Logos.')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(salas='Quantidade total de salas (padrão 6)', inatividade='Minutos sem mensagens para liberar (padrão 30)',
                           ajudantes='Cargo chamado uma vez após 15 minutos', disponiveis='Categoria disponível existente, opcional',
                           ocupados='Categoria ocupada existente, opcional')
    async def configure(self, interaction: discord.Interaction, salas: app_commands.Range[int, 1, 20] = 6,
                        inatividade: app_commands.Range[int, 5, 1440] = 30,
                        ajudantes: discord.Role | None = None, disponiveis: discord.CategoryChannel | None = None,
                        ocupados: discord.CategoryChannel | None = None):
        guild = interaction.guild
        if not guild or not guild.me:
            return
        if not guild.me.guild_permissions.manage_channels:
            await interaction.response.send_message('Preciso de Gerenciar canais para criar e mover as salas.', ephemeral=True)
            return
        if ajudantes and ajudantes.is_default():
            await interaction.response.send_message('Escolha um cargo específico, não @everyone.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.locks[guild.id]:
            state = await self.state(guild.id)
            available_id = disponiveis.id if disponiveis else state.get('available_category_id')
            occupied_id = ocupados.id if ocupados else state.get('occupied_category_id')
            if available_id and available_id == occupied_id:
                await interaction.followup.send('Disponíveis e Ocupados precisam ser categorias diferentes.', ephemeral=True)
                return
            # Categorias novas são públicas, com regras explícitas; categorias
            # existentes mantêm suas permissões e podem restringir a audiência.
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                            read_message_history=True, mention_everyone=False),
                          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                            read_message_history=True, manage_channels=True, manage_messages=True,
                            mention_everyone=True)}
            try:
                for field, supplied, name in [('available_category_id', disponiveis, '✅ Logos — Ajuda disponível'),
                                               ('occupied_category_id', ocupados, '⌛ Logos — Ajuda ocupada')]:
                    category = supplied or guild.get_channel(state.get(field))
                    if not isinstance(category, discord.CategoryChannel):
                        category = await guild.create_category(name, overwrites=overwrites, reason='Configuração de ajuda Logos')
                    state[field] = category.id
                    await self.save(guild.id, state)
                if state['available_category_id'] == state['occupied_category_id']:
                    raise RuntimeError('Disponíveis e Ocupados precisam ser categorias diferentes.')
                state.update(inactivity_minutes=inatividade, enabled=True)
                if ajudantes:
                    state['helper_role_id'] = ajudantes.id
                await self.save(guild.id, state)
                category = guild.get_channel(state['available_category_id'])
                # Remover registros de canais excluídos não exclui canais existentes.
                state['channels'] = {key: room for key, room in state['channels'].items() if guild.get_channel(int(key))}
                await self.save(guild.id, state)
                guide = guild.get_channel(state.get('guide_channel_id'))
                if not isinstance(guide, discord.TextChannel):
                    guide_overwrites = dict(category.overwrites)
                    everyone = guide_overwrites.get(guild.default_role, discord.PermissionOverwrite())
                    everyone.update(send_messages=False)
                    guide_overwrites[guild.default_role] = everyone
                    guide = await guild.create_text_channel('como-pedir-ajuda', category=category, overwrites=guide_overwrites,
                                                            reason='Instruções de ajuda Logos')
                    state['guide_channel_id'] = guide.id
                    await self.save(guild.id, state)
                prefix = await self.prefix(guild)
                guide_id = state.get('guide_message_id')
                text = instructions(prefix, inatividade)
                if guide_id:
                    try:
                        await guide.get_partial_message(guide_id).edit(content=text, allowed_mentions=NONE)
                    except discord.NotFound:
                        guide_id = None
                if not guide_id:
                    sent = await guide.send(text, allowed_mentions=NONE)
                    state['guide_message_id'] = sent.id
                    await self.save(guild.id, state)
                number = 1
                while len(state['channels']) < salas:
                    names = {room['base_name'] for room in state['channels'].values()}
                    while f'help-{number}' in names:
                        number += 1
                    channel = await guild.create_text_channel(f'help-{number}', category=category, reason='Sala pública de ajuda Logos')
                    room = {'base_name': channel.name, 'owner_id': None}
                    state['channels'][str(channel.id)] = room
                    await self.save(guild.id, state)
                    await self.enact(guild, state, channel, room)
                    await self.say(channel, f'**Sala disponível.** Publique sua dúvida para reservar. Leia <#{guide.id}>.')
                for key, room in state['channels'].items():
                    channel = guild.get_channel(int(key))
                    if isinstance(channel, discord.TextChannel):
                        await self.enact(guild, state, channel, room)
            except (discord.HTTPException, RuntimeError, asyncio.TimeoutError) as exc:
                log.exception('Configuração de ajuda Logos incompleta')
                await interaction.followup.send(f'Configuração parcial salva. Confira minhas permissões e tente novamente. Detalhe: {str(exc)[:250]}', ephemeral=True)
                return
            await interaction.followup.send(f'Ajuda Logos ativa: {len(state["channels"])} salas; fechamento após {inatividade} minutos de inatividade. '
                                             f'Guia: <#{state["guide_channel_id"]}>. Categorias: <#{state["available_category_id"]}> e <#{state["occupied_category_id"]}>.', ephemeral=True)

    @logos_ajuda.command(name='status', description='Mostra a configuração e as reservas do Logos.')
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        state = await self.state(interaction.guild_id)
        occupied = sum(bool(room.get('owner_id')) for room in state['channels'].values())
        await interaction.response.send_message(f'Ajuda Logos: {"ativa" if state["enabled"] else "pausada"}. '
            f'{len(state["channels"])} salas, {occupied} ocupadas. Inatividade: {state["inactivity_minutes"]} min. '
            f'Cargo de ajudantes: {"configurado" if state["helper_role_id"] else "não configurado"}.', ephemeral=True)

    @logos_ajuda.command(name='pausar', description='Pausa novas reservas e o fechamento automático, mantendo as salas.')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def pause(self, interaction: discord.Interaction):
        async with self.locks[interaction.guild_id]:
            state = await self.state(interaction.guild_id)
            state['enabled'] = False
            await self.save(interaction.guild_id, state)
        await interaction.response.send_message('Novas reservas e fechamento automático pausados. O fechamento manual continua disponível. Use configurar para retomar.', ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        async with self.locks[channel.guild.id]:
            state = await self.state(channel.guild.id)
            if state['channels'].pop(str(channel.id), None) is not None:
                await self.save(channel.guild.id, state)
            self._activity.pop(channel.id, None)
            self._desired_names.pop(channel.id, None)
            task = self._rename_tasks.get(channel.id)
            if task:
                task.cancel()

    @tasks.loop(seconds=30)
    async def scheduler(self):
        for guild in self.bot.guilds:
            try:
                await self.process_guild(guild)
            except Exception:
                log.exception('Falha ao reconciliar salas Logos do servidor %s', guild.id)

    @scheduler.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()

    async def process_guild(self, guild: discord.Guild):
        async with self.locks[guild.id]:
            state = await self.state(guild.id)
            activity_changed = False
            for key, room in state['channels'].items():
                latest = self._activity.get(int(key), 0)
                if room.get('owner_id') and latest > room.get('last_activity', 0):
                    room['last_activity'] = latest
                    activity_changed = True
            if activity_changed:
                await self.save(guild.id, state)
            for key, room in list(state['channels'].items()):
                channel = guild.get_channel(int(key))
                if not isinstance(channel, discord.TextChannel):
                    state['channels'].pop(key)
                    await self.save(guild.id, state)
                    continue
                try:
                    # Após reinício, mensagens humanas enviadas enquanto o bot
                    # estava offline contam para o prazo de inatividade.
                    if state['enabled'] and room.get('owner_id') and not room.get('closing') and channel.id not in self._recovered_channels:
                        latest = float(room.get('last_activity', 0))
                        async for message in channel.history(limit=25, oldest_first=False):
                            if not message.author.bot and not message.webhook_id:
                                latest = max(latest, min(time.time(), message.created_at.timestamp()))
                        room['last_activity'] = latest
                        await self.save(guild.id, state)
                        self._recovered_channels.add(channel.id)
                    if room.get('closing'):
                        await self.finish_release(guild, state, channel, room)
                    elif state['enabled'] and room.get('owner_id') and time.time() - room.get('last_activity', time.time()) >= state['inactivity_minutes'] * 60:
                        await self.release(guild, state, channel, room, 'fechada por inatividade')
                    elif state['enabled']:
                        await self.enact(guild, state, channel, room)
                except discord.NotFound:
                    state['channels'].pop(key, None)
                    self._activity.pop(channel.id, None)
                    await self.save(guild.id, state)
                except (discord.HTTPException, RuntimeError, asyncio.TimeoutError):
                    log.warning('Sala Logos %s pendente; será tentada novamente.', channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(LogosHelp(bot))
