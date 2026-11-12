"""Discord playback messages and voice-channel authorization."""
import datetime

import discord

from youtube import track_url


def get_now_playing_embed(data):
    title = data.get('title', '未知標題')
    duration = data.get('duration')
    duration_str = str(datetime.timedelta(seconds=int(duration))) if duration else '直播/未知'
    embed = discord.Embed(
        title='🎵 正在播放', description=f'[{title}]({track_url(data)})', color=0x1db954,
    )
    if data.get('thumbnail'):
        embed.set_thumbnail(url=data['thumbnail'])
    embed.add_field(name='⏱️ 時間', value=duration_str, inline=True)
    return embed


async def ensure_voice(interaction):
    if not interaction.guild:
        await interaction.response.send_message('❌ 請在伺服器中使用此指令', ephemeral=True)
        return None, None
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message('❌ 我不在語音頻道中', ephemeral=True)
        return None, None
    voice = getattr(interaction.user, 'voice', None)
    if not voice or voice.channel != vc.channel:
        await interaction.response.send_message('❌ 請先加入與機器人相同的語音頻道', ephemeral=True)
        return None, None
    return vc, interaction.guild_id


