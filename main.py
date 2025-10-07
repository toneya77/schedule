import os
import discord
import aiohttp
from discord import app_commands, ui
from discord.ext import commands, tasks
from typing import Literal, Optional
import datetime
import pandas as pd # 日付計算を容易にするため使用

# --- 環境変数の読み込み ---
DISCORD_BOT_TOKEN = os.environ.get("MTQyNTA0NTI3MzE3MTg1NzU1OA.G1_QNN.tissoIRxRHTe98P-RkCki6GJKy5MoH8wqTZlYs")
GAS_WEB_APP_URL = os.environ.get("https://script.google.com/macros/s/AKfycby7kMZDiWppPcOYqWyJm148Qn2dy6pNwU6vVlVdJZJ-klal3HFbywTLxP9RVlDv36GX/exec")
SECRET_TOKEN = os.environ.get("MTQyNTA0NTI3MzE3MTg1NzU1OA.G1_QNN.tissoIRxRHTe98P-RkCki6GJKy5MoH8wqTZlYs")

if not all([DISCORD_BOT_TOKEN, GAS_WEB_APP_URL, SECRET_TOKEN]):
    print("FATAL: 必要な環境変数が設定されていません。Botは起動できません。")
    # exit() 

# --- Discord Botのセットアップ ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 共通ユーティリティ/定数 ---
STATUS_CHOICES = ['未着手', '動画UP済み', 'メモ記入済み', '提出済み']
STATUS_EMOJIS = {'未着手': '⚪', '動画UP済み': '🎬', 'メモ記入済み': '📝', '提出済み': '✅'}
# 通知設定のキャッシュ (GASからロードされ、再起動後も永続化される)
NOTIFICATION_SETTINGS = {} 

# GAS通信関数 (変更なし)
async def send_gas_request(action: str, payload: dict = None):
    """Google Apps Script (GAS) Web App に HTTP POST リクエストを送信する。"""
    data = {
        "token": SECRET_TOKEN,
        "action": action,
        "payload": payload if payload is not None else {}
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GAS_WEB_APP_URL, json=data) as response:
                response.raise_for_status() 
                return await response.json()
        except Exception as e:
            return {"error": f"GAS通信エラー: {e.__class__.__name__}: {e}"}

# View/Component クラス (StatusSelect, StatusView) は長いため省略。
# 前回のコードの定義を引き継いでください。
# --- View / Component (提出状況の変更用プルダウン) ---
class StatusSelect(ui.Select):
    """提出状況を変更するためのプルダウンメニュー"""
    def __init__(self, schedule_id: int, current_status: str):
        self.schedule_id = schedule_id
        options = []
        for status in STATUS_CHOICES:
            is_default = (status == current_status)
            options.append(discord.SelectOption(
                label=status, 
                value=status, 
                emoji=STATUS_EMOJIS.get(status),
                default=is_default
            ))
        
        super().__init__(
            placeholder=f"現在の状況: {current_status}",
            min_values=1, max_values=1, options=options,
            custom_id=f"status_select_{schedule_id}"
        )

    async def callback(self, interaction: discord.Interaction):
        new_status = self.values[0]
        await interaction.response.defer(ephemeral=True)

        payload = {
            "id": self.schedule_id,
            "field": "提出状況",
            "value": new_status
        }
        
        response = await send_gas_request("edit_value", payload)

        if response.get("success"):
            await interaction.followup.send(
                f"{STATUS_EMOJIS.get(new_status)} ID `{self.schedule_id}` の提出状況を **{new_status}** に変更しました。", 
                ephemeral=True
            )
        else:
            error_msg = response.get("error", "不明なエラーが発生しました。")
            await interaction.followup.send(f"❌ 状況の変更に失敗しました。\nエラー: `{error_msg}`", ephemeral=True)

class StatusView(ui.View):
    """プルダウンメニューを保持するView"""
    def __init__(self, schedule_id: int, current_status: str):
        super().__init__(timeout=300) 
        self.add_item(StatusSelect(schedule_id, current_status))
# --- ここまで View/Component ---

# --- Botイベント ---

@bot.event
async def on_ready():
    """Botが起動し、Discordに接続したときに実行される"""
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    # 起動時に通知設定をGASから読み込む
    await load_notification_settings()
    # リマインダーチェックタスクを開始
    # Botが実行されている時刻に毎日実行されます
    check_reminders.start()

async def load_notification_settings():
    """GASから通知設定を読み込み、NOTIFICATION_SETTINGSにキャッシュする"""
    global NOTIFICATION_SETTINGS
    response = await send_gas_request("settings_load")
    if response.get("success") and response.get("settings"):
        settings = response["settings"]
        # GASからは文字列として読み込まれるため型変換
        if settings.get('NotificationDays'):
             settings['NotificationDays'] = int(settings['NotificationDays'])
        
        NOTIFICATION_SETTINGS = settings
        print(f"Loaded settings: {NOTIFICATION_SETTINGS}")
    else:
        print(f"No settings found or error loading settings: {response.get('error')}")


# --- 定期実行タスク ---

@tasks.loop(hours=24) # 毎日24時間ごと (Botが実行を開始した時刻の24時間後) に実行
async def check_reminders():
    """提出期限が迫っている未完了の予定をチェックし、通知する"""
    
    if not NOTIFICATION_SETTINGS or not NOTIFICATION_SETTINGS.get('NotificationDays') or not NOTIFICATION_SETTINGS.get('ChannelID'):
        # 設定がない場合はスキップ
        return

    days_before = NOTIFICATION_SETTINGS['NotificationDays']
    channel_id = NOTIFICATION_SETTINGS['ChannelID']
    today = datetime.date.today()
    reminder_list = []

    # 1. スケジュール一覧を取得
    list_response = await send_gas_request("list")
    if not list_response.get("success"):
        print(f"Error fetching schedules for reminder check: {list_response.get('error')}")
        return

    schedules = list_response.get("schedules", [])

    # 2. リマインダーロジック
    for schedule in schedules:
        due_date_str = str(schedule.get('due_date'))
        status = schedule.get('status')
        
        # 提出状況が「提出済み」ではない、かつ提出日が存在する場合のみ処理
        if status != '提出済み' and due_date_str and due_date_str != 'None':
            try:
                # 日付文字列をパース
                due_date = pd.to_datetime(due_date_str, errors='raise').date() 
                
                # 提出期限までの残り日数を計算
                days_left = (due_date - today).days

                # 期限が days_before 日以内、かつ今日以降の場合 (days_left >= 0)
                if 0 <= days_left <= days_before:
                    # 通知メッセージで残り日数を使いたいので、辞書にDaysLeftを追加
                    schedule['DaysLeft'] = days_left
                    reminder_list.append(schedule)

            except Exception:
                # 日付フォーマットエラーはスキップ
                continue

    # 3. 通知の送信
    if reminder_list:
        target_channel = bot.get_channel(int(channel_id))
        if not target_channel:
            print(f"Error: Target channel ID {channel_id} not found.")
            return

        notification_embed = discord.Embed(
            title=f"🚨 提出期限間近のリマインダー ({days_before}日前)",
            description=f"提出期限が迫っている、未完了の予定が {len(reminder_list)} 件あります。",
            color=discord.Color.red()
        )
        
        for schedule in reminder_list:
            status_emoji = STATUS_EMOJIS.get(schedule['status'], '❓')
            
            title_field = f"{status_emoji} ID `{schedule['id']}`: {schedule['title']}"
            
            details = (
                f"**提出日:** `{schedule['due_date']}` (残り **{schedule['DaysLeft']}** 日)\n"
                f"**撮影日:** `{schedule['shoot_date']}`\n"
                f"**提出状況:** `{schedule['status'] or '未記入'}`\n"
                f"**ファイル:** {f'[Link]({schedule['file_url']})' if schedule['file_url'] else 'なし'}\n"
                f"**YouTube:** {f'[Link]({schedule['yt_url']})' if schedule['yt_url'] else 'なし'}\n"
                f"**⚠️ この予定は、まだ提出されていません！**"
            )
            
            notification_embed.add_field(name=title_field, value=details, inline=False)
            
        await target_channel.send(embed=notification_embed)


# --- スラッシュコマンドの実装 ---

@bot.tree.command(name="scadd", description="新しい予定をスプレッドシートに追加します。")
@app_commands.describe(
    title="予定のタイトル (必須)",
    shoot_date="撮影日 (例: 2025/10/01)",
    due_date="提出期限 (例: 2025/10/10)",
    file_url="ギガファイル便などのURL",
    yt_url="YouTube動画のURL"
)
async def scadd(
    interaction: discord.Interaction, 
    title: str, 
    shoot_date: str, 
    due_date: Optional[str] = "", 
    file_url: Optional[str] = "", 
    yt_url: Optional[str] = ""
):
    await interaction.response.defer(ephemeral=True)
    
    payload = {
        "title": title,
        "shoot_date": shoot_date,
        "due_date": due_date,
        "file_url": file_url,
        "yt_url": yt_url
    }
    
    response = await send_gas_request("add", payload)
    
    if response.get("success"):
        embed = discord.Embed(
            title="✅ 予定の追加に成功しました",
            description=f"予定 **{response['title']}** を登録しました。",
            color=discord.Color.green()
        )
        embed.add_field(name="割り当てID", value=f"`{response['id']}`", inline=True)
        embed.add_field(name="撮影日", value=shoot_date, inline=True)
        embed.add_field(name="提出状況 (初期値)", value="未着手", inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=False) 
    else:
        error_msg = response.get("error", "不明なエラーが発生しました。")
        await interaction.followup.send(f"❌ 予定の追加に失敗しました。\nエラー: `{error_msg}`", ephemeral=True)

@bot.tree.command(name="sclist", description="登録されている予定の一覧を表示します。")
async def sclist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    
    response = await send_gas_request("list")
    
    if response.get("success"):
        schedules = response.get("schedules")
        
        if not schedules:
            await interaction.followup.send("📝 現在、登録されている予定はありません。`/scadd`で追加してください。", ephemeral=False)
            return

        embed = discord.Embed(
            title="📅 予定・提出管理リスト",
            description=f"現在 **{len(schedules)}** 件の予定が登録されています。",
            color=discord.Color.blue()
        )
        
        for i, schedule in enumerate(schedules):
            if i >= 25: 
                break

            status_emoji = STATUS_EMOJIS.get(schedule['status'], '❓')
            title = f"{status_emoji} ID `{schedule['id']}`: {schedule['title']}"
            
            details = (
                f"**撮影日:** `{schedule['shoot_date']}`\n"
                f"**提出期限:** `{schedule['due_date'] or '未設定'}`\n"
                f"**提出状況:** `{schedule['status'] or '未記入'}`\n"
                f"**ファイル:** {f'[Link]({schedule['file_url']})' if schedule['file_url'] else 'なし'}\n"
                f"**YouTube:** {f'[Link]({schedule['yt_url']})' if schedule['yt_url'] else 'なし'}"
            )
            
            embed.add_field(name=title, value=details, inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=False)

    else:
        error_msg = response.get("error", "不明なエラーが発生しました。")
        await interaction.followup.send(f"❌ 予定一覧の取得に失敗しました。\nエラー: `{error_msg}`", ephemeral=True)

@bot.tree.command(name="scsitu", description="指定したIDの予定の提出状況をプルダウンで変更します。")
@app_commands.describe(schedule_id="変更したい予定のID")
async def scsitu(interaction: discord.Interaction, schedule_id: int):
    await interaction.response.defer(ephemeral=True)

    list_response = await send_gas_request("list")

    if not list_response.get("success"):
        await interaction.followup.send(f"❌ 予定情報の取得に失敗しました。\nエラー: `{list_response.get('error')}`", ephemeral=True)
        return

    schedules = list_response.get("schedules", [])
    target_schedule = next((s for s in schedules if s['id'] == schedule_id), None) 

    if not target_schedule:
        await interaction.followup.send(f"❌ ID `{schedule_id}` の予定が見つかりませんでした。", ephemeral=True)
        return

    current_status = target_schedule['status']
    
    view = StatusView(schedule_id, current_status)
    
    embed = discord.Embed(
        title=f"📝 提出状況の変更 (ID: {schedule_id})",
        description=f"**予定名:** `{target_schedule['title']}`\n\nプルダウンから新しい提出状況を選択してください。",
        color=discord.Color.orange()
    )

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="scedit", description="指定したIDの予定の任意の項目を編集します。")
@app_commands.describe(
    schedule_id="編集したい予定のID",
    field_name="編集したい項目名",
    new_value="新しい値"
)
async def scedit(
    interaction: discord.Interaction, 
    schedule_id: int, 
    field_name: Literal['予定名', '撮影日', '提出日', 'ファイルURL', 'YTURL'], 
    new_value: str
):
    await interaction.response.defer(ephemeral=True)
    
    payload = {
        "id": schedule_id,
        "field": field_name,
        "value": new_value
    }
    
    response = await send_gas_request("edit_value", payload)
    
    if response.get("success"):
        await interaction.followup.send(
            f"✅ 編集に成功しました。\nID `{schedule_id}` の **{field_name}** を **{new_value}** に更新しました。", 
            ephemeral=False
        )
    else:
        error_msg = response.get("error", "不明なエラーが発生しました。")
        await interaction.followup.send(f"❌ 編集に失敗しました。\nエラー: `{error_msg}`", ephemeral=True)

@bot.tree.command(name="screm", description="提出期限前の通知日数とチャンネルを設定します。")
@app_commands.describe(
    days="提出日の何日前に通知するか (例: 2)",
    channel="通知メッセージを送信するチャンネル"
)
async def screm(interaction: discord.Interaction, days: int, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)

    if days <= 0:
        await interaction.followup.send("❌ 通知日数は1日以上で指定してください。", ephemeral=True)
        return

    payload = {
        "NotificationDays": days,
        "ChannelID": str(channel.id)
    }
    
    # GASに設定を保存
    response = await send_gas_request("settings_save", payload)
    
    if response.get("success"):
        # Botの内部キャッシュを更新
        global NOTIFICATION_SETTINGS
        NOTIFICATION_SETTINGS = {"NotificationDays": days, "ChannelID": str(channel.id)}
        
        embed = discord.Embed(
            title="🔔 リマインダー設定完了",
            description="提出期限前の通知設定を保存しました。Botは毎日自動でチェックします。",
            color=discord.Color.blue()
        )
        embed.add_field(name="通知日数", value=f"`{days}日前`", inline=True)
        embed.add_field(name="通知先チャンネル", value=channel.mention, inline=True)
        embed.set_footer(text="Botがダウンしない限り、設定は永続化されます。")

        await interaction.followup.send(embed=embed, ephemeral=False)
        # 設定が完了したらタスクが実行されるように、一度停止して再開する
        if check_reminders.is_running():
            check_reminders.restart()
        else:
            check_reminders.start()
    else:
        error_msg = response.get("error", "不明なエラーが発生しました。")
        await interaction.followup.send(f"❌ 設定の保存に失敗しました。\nエラー: `{error_msg}`", ephemeral=True)

# --- Botの起動 ---
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
