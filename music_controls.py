"""Discord playback controls, routed to the bot's shared command handlers."""
import logging

import discord

logger = logging.getLogger('MusicBot')


class PlaybackControls(discord.ui.View):
    def __init__(self, owner):
        super().__init__(timeout=None)
        self.owner = owner

    async def interaction_check(self, interaction):
        return await self.owner.authorize_controls(interaction)

    @discord.ui.button(label='暫停／繼續', emoji='⏯️', style=discord.ButtonStyle.primary, custom_id='music:toggle')
    async def toggle(self, interaction, button):
        await self.owner.handle_control_action(interaction, 'toggle', private=True)

    @discord.ui.button(label='下一首', emoji='⏭️', style=discord.ButtonStyle.secondary, custom_id='music:skip')
    async def skip_next(self, interaction, button):
        await self.owner.handle_control_action(interaction, 'skip', private=True)

    @discord.ui.button(label='隨機排序', emoji='🔀', style=discord.ButtonStyle.secondary, custom_id='music:shuffle')
    async def shuffle(self, interaction, button):
        await self.owner.handle_control_action(interaction, 'shuffle', private=True)

    @discord.ui.button(label='停止', emoji='⏹️', style=discord.ButtonStyle.danger, custom_id='music:stop')
    async def stop_playback(self, interaction, button):
        await self.owner.handle_control_action(interaction, 'stop', private=True)

    async def on_error(self, interaction, error, item):
        logger.error('播放控制失敗', exc_info=(type(error), error, error.__traceback__))
        try:
            if interaction.response.is_done():
                await interaction.followup.send('⚠️ 操作失敗，請稍後重試。', ephemeral=True)
            else:
                await interaction.response.send_message('⚠️ 操作失敗，請稍後重試。', ephemeral=True)
        except discord.HTTPException:
            pass
