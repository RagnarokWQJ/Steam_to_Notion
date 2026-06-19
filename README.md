# Steam到Notion同步工具

这个目录是独立项目，不依赖`steam2notion`中的其他文件。

## 功能

- 从Steam Web API抓取指定用户拥有的游戏，并用最近游玩游戏补充家庭共享游戏。
- 从Notion游戏总表读取历史游戏数据。
- 按`AppID`新增或更新Notion游戏总表。
- 将每次同步时的当前总时长和相对上一次的差值写入时长记录表。
- 非重复统计日查询Steam成就，并同步总成就数和已完成成就数。
- 为游戏总表写入Steam封面URL和图标URL，并把Notion页面cover和icon设置为对应图片。
- Notion里多出来的游戏只输出报告，不删除、不归档。
- 程序会尽量捕获异常并继续运行，GitHub Actions最终退出码保持`0`。

## 环境变量

必须在GitHub Actions Secrets中配置：

- `STEAM_API_KEY`
- `STEAM_ID64`
- `NOTION_API_KEY`
- `NOTION_GAME_DATA_SOURCE_ID`
- `NOTION_PLAYTIME_DATA_SOURCE_ID`

可选：

- `NOTION_PERIOD_DATA_SOURCE_ID`，月度/年度统计表data source id；未配置时会跳过周期统计，但不影响基础时长记录写入。
- `NOTION_SUMMARY_DATA_SOURCE_ID`，月度/年度总结表data source id；未配置时会跳过总结关联，但不影响基础时长记录写入。
- `NOTION_VERSION`，默认值为`2025-09-03`。
- `STEAM_SYNC_STATE_FILE`，默认值为`steam_sync`目录下的`.steam_sync_state/state.json`。
- `STEAM_ACHIEVEMENT_MAX_WORKERS`，默认值为`5`，表示成就查询并发线程数。
- `STEAM_ACHIEVEMENT_REQUEST_INTERVAL_SECONDS`，默认值为`0.2`，表示所有成就请求共享同一个启动间隔，默认最多每秒5个请求。

代码不会读取`.env`。测试阶段如果在`run_sync()`中显式写死`Config(...)`，程序会优先使用该代码块。

## Notion字段要求

游戏总表需要这些字段：

| 字段名 | 类型 |
| --- | --- |
| `Name` | title |
| `AppID` | number |
| `TotalPlaytimeMinutes` | number |
| `StoreUrl` | url |
| `HeaderImageUrl` | url |
| `IconImageUrl` | url |
| `UnrecordPlaytimeMinutes` | number |
| `RecentPlayed` | number，`1`表示本次`GetRecentlyPlayedGames`包含该游戏，否则为`0` |
| `TotalAchievement` | number |
| `AchievedAchievement` | number |
| `MonthSummaryRelation` | relation，关联到月度/年度总结表的月度条目 |
| `YearSummaryRelation` | relation，关联到月度/年度总结表的年度条目 |
| `PlayedYear` | multi_select，格式为`YYYY` |
| `PlayedMonth` | multi_select，格式为`YYYY-MM` |
| `BuyYear` | formula，购买年份，输出数字`YYYY` |
| `BuyMonth` | formula，购买年月，输出数字`YYYYMM`或文本`YYYY-MM` |
| `CompleteYear` | formula，通关年份，输出数字`YYYY` |
| `CompleteMonth` | formula，通关年月，输出数字`YYYYMM`或文本`YYYY-MM` |
| `FullAchievementYear` | formula，全成就年份，输出数字`YYYY` |
| `FullAchievementMonth` | formula，全成就年月，输出数字`YYYYMM`或文本`YYYY-MM` |

时长记录表需要这些字段：

| 字段名 | 类型 |
| --- | --- |
| `Name` | title |
| `AppID` | number |
| `Date` | date |
| `TotalPlaytimeMinutes` | number |
| `DeltaMinutes` | number |
| `GameLogRelation` | relation，关联到游戏总表 |
| `PeriodRelation` | relation，关联到月度/年度统计表 |

月度/年度统计表需要这些字段：

| 字段名 | 类型 |
| --- | --- |
| `Name` | title |
| `PeriodID` | rich_text |
| `Period` | select，例如`2026`或`2026-06` |
| `Type` | select，选项为`Year`或`Month` |
| `Year` | select，例如`2026` |
| `Month` | select，选项为`1`到`12`，年度记录为空 |
| `AppID` | number |
| `PlayTimeMinutes` | number |
| `GameLogRelation` | relation，关联到游戏总表 |
| `SummaryRelation` | relation，关联到月度/年度总结表 |

月度/年度总结表需要这些字段：

| 字段名 | 类型 |
| --- | --- |
| `Period` | title，例如`2026`或`2026-06` |
| `Type` | select，选项为`Year`或`Month` |
| `NewGameNum` | number |
| `CompleteGameNum` | number |
| `FullAchievementGameNum` | number |
| `PlayedGameNum` | number |
| `TotalPlayTimeMinutes` | number |

## 同步规则

- 使用`AppID`作为唯一键。
- 每次同步会同时查询`GetOwnedGames`和`GetRecentlyPlayedGames`。
- `GetOwnedGames`结果优先，`GetRecentlyPlayedGames`只补充前者没有的`AppID`。
- 家庭共享游戏只有最近玩过时才可能通过`GetRecentlyPlayedGames`进入同步列表。
- 每次同步会按本次`GetRecentlyPlayedGames`结果全库同步`RecentPlayed`，包含的游戏目标值为`1`，其他游戏目标值为`0`；如果当前值已经等于目标值会跳过写入，如果该接口失败则目标值全部按`0`处理。
- Steam有、Notion没有：新增游戏总表记录。
- 如果Notion游戏总表已有`HeaderImageUrl`，程序不会处理该字段。
- 如果`HeaderImageUrl`为空，程序会请求`https://store.steampowered.com/api/appdetails?appids={appid}&cc=us`，读取`{appid}.data.header_image`写入该字段。
- 如果`appdetails`请求失败或没有返回`header_image`，程序会尝试回退到`https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg`。
- 回退URL只有在能成功返回图片内容时才会写入`HeaderImageUrl`，否则保持为空。
- 每个游戏的`IconImageUrl`由Steam返回的`img_icon_url`拼接为`http://media.steampowered.com/steamcommunity/public/images/apps/{appid}/{img_icon_url}.jpg`。
- Notion页面cover使用同一个封面URL，画廊视图可以选择页面封面作为卡片封面。
- Notion页面icon使用同一个图标URL，画廊视图可以显示页面图标。
- 如果状态文件不存在、为空、损坏或没有有效`last_update_date`，本次运行会被视为第一次初始化。
- 第一次初始化会更新总时长、成就和`UnrecordPlaytimeMinutes`，但不会写入任何时长记录。
- 如果本次日期还没有处理过时长：更新总时长和成就字段；新增游戏会写入一条时长记录，`DeltaMinutes = TotalPlaytimeMinutes`。
- 非第一次运行时，新游戏总时长超过480分钟会被视为历史未记录数据，只写入游戏总表和`UnrecordPlaytimeMinutes`，不写时长记录。
- 新写入的时长记录会在`GameLogRelation`字段中关联到相同`AppID`的游戏总表页面。
- 正常写入时长记录时，会同步年度和月度统计表，并在时长记录的`PeriodRelation`字段中关联对应的两条统计记录。
- 年度统计`PeriodID`格式为`YYYY_AppID`，月度统计`PeriodID`格式为`YYYY-MM_AppID`。
- 正常写入时长记录时，会确保对应的月度/年度总结条目存在；周期统计条目会写入`SummaryRelation`。
- 游戏总表会追加对应的`MonthSummaryRelation`、`YearSummaryRelation`、`PlayedYear`和`PlayedMonth`，用于Notion分组和筛选。
- 每次同步结束前会全量重算月度/年度总结表的`NewGameNum`、`CompleteGameNum`、`FullAchievementGameNum`、`PlayedGameNum`和`TotalPlayTimeMinutes`，不是增量累加。
- 总结数字中，购买、通关、全成就数量来自游戏总表的`BuyYear`/`BuyMonth`、`CompleteYear`/`CompleteMonth`、`FullAchievementYear`/`FullAchievementMonth`；游玩数量来自月度/年度统计表中相同`Period + Type`的条目数；总游玩时长来自这些条目的`PlayTimeMinutes`总和。
- 如果本次日期已经处理过时长：只更新游戏名称、商店链接、缺失的封面URL、图标URL、页面cover和页面icon，不更新总时长和成就字段，也不写时长记录。
- 成就接口成功且包含`achievements`时，统计总成就数和已完成成就数。
- 成就接口成功但没有`achievements`时，写入`0 / 0`。
- 成就接口失败或返回结构异常时，不覆盖Notion旧成就字段，只输出日志。
- 总时长小于Notion旧值时，输出异常，不写时长记录。
- Notion有、两个Steam接口都没有：只输出日志，不修改该页面；这类数据可能是很久没玩的家庭共享游戏。

## 跨GitHub Actions运行状态

程序用状态文件保存上次处理时长和成就的日期：

```json
{
  "last_update_date": "2026-06-17"
}
```

GitHub Actions workflow使用`actions/cache`保存`.steam_sync_state`目录。这样手动触发同一天的workflow时，程序可以识别当天已经处理过时长和成就，只更新游戏基础条目、封面和图标，避免重复生成时长记录或重复发送大量成就查询请求。

状态文件只包含日期，不包含任何密钥。目录内的`.gitignore`会忽略`.steam_sync_state/`，避免本地运行后误提交。

## Notion维护工具

修复已有时长记录的`GameLogRelation`字段：

```bash
python notion_tools.py repair-relations ^
  --game-data-source-id <游戏总表data_source_id> ^
  --playtime-data-source-id <时长记录表data_source_id>
```

将指定模板应用到指定data source下的所有页面：

```bash
python notion_tools.py apply-template ^
  --data-source-id <目标data_source_id> ^
  --template-id <模板id>
```

如果需要先清空页面内容再应用模板，可以显式追加`--erase-content`。这个选项会删除页面现有内容，使用前需要确认。

工具默认从`NOTION_API_KEY`读取密钥，也可以用`--notion-api-key`覆盖；`NOTION_VERSION`默认值与主程序一致。

## 本地运行

```bash
pip install -r requirements.txt
python main.py
```

如果没有配置环境变量，程序会输出缺失项并正常结束。

## GitHub Actions

仓库根目录为`steam_sync`时，项目已经提供`.github/workflows/steam-to-notion.yml`。上传到GitHub后会在北京时间每天凌晨2点自动执行，并支持在Actions页面通过`workflow_dispatch`手动启动。

GitHub cron使用UTC时间，所以北京时间`02:00`对应workflow中的`0 18 * * *`。

正式部署前请确认`run_sync()`没有继续使用测试阶段写死的`Config(...)`覆盖环境变量，否则GitHub Actions传入的Secrets不会生效。
