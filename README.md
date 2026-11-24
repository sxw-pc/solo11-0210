# 社区球场雨后赛程补场采录台

社区积分联赛的值班人员用本地 JSON 批次采录雨情、联赛资格、临时占场、补赛承诺及业已完成的比分。台账按轮发布积分，并保留迟到证据与冲回记录。示例数据为虚构。

要求 Python 3.11 或更新版本。进入本项目目录即可运行，无依赖安装。

```text
python -m courts_rain --help
python -m courts_rain demo --db demo.sqlite
python -m courts_rain round-report --db demo.sqlite r1
python -m courts_rain audit --db demo.sqlite
python -m unittest discover -s tests -v
```

分阶段观察积分快照与回退时，可另用空库运行以下命令。`apply` 自动初始化 SQLite，`init --db league.sqlite` 可单独初始化。

```text
python -m courts_rain apply --db league.sqlite fixtures/league.json
python -m courts_rain standings --db league.sqlite
python -m courts_rain court-status --db league.sqlite east 100
python -m courts_rain availability --db league.sqlite ref-a 160 190
python -m courts_rain apply --db league.sqlite fixtures/late-recall.json
python -m courts_rain audit --db league.sqlite
```

## 采录与裁定

每条 JSON 事件有全库唯一 `id` 和 `op`。`apply` 以一个文件为原子批次，按数组顺序执行。完全相同的事件重传返回 `duplicate`，异内容重用事件号会拒绝整个批次。所有时间为同一联赛时钟的非负整数分钟，场次与参与者可用区间按左闭右开计算。

| 事件 | 裁定 |
| --- | --- |
| `court`、`inspect` | 最新且在目标分钟有效的巡查按面层判断。丙烯酸湿度上限 0，有效期 60 分钟。透水面上限 1、90 分钟。合成面上限 2、45 分钟。杂物一律阻断。同球场同分钟只接纳一条巡查。 |
| `division`、`team`、`official`、`window` | 分组保存积分、时长和同轮开赛跨度。球队和裁判须归属适用分组。相邻的可用窗口可拼接，任何重叠停用窗口均覆盖可用声明。 |
| `round`、`fixture`、`assign` | 固定轮次窗口和场次数。同一球队每轮只入一场，落位时查所有轮次当前占场及人员冲突。 |
| `promise`、`consent`、`move` | 承诺绑定原占场版本及新场、新时间、裁判和有效期。两队及新裁判须在兑现分钟前确认。旧场的开赛分钟须已封闭，新场巡查须在兑现时已发生并覆盖新开赛分钟。目标冲突时保留旧占场。 |
| `result` | 成绩绑定当前占场版本，结束后递增修订号。两个 `null` 成绩作废现有比分。活动结算须先冲回，之后才可修订。 |
| `settle`、`recall` | 完整轮次、有效比分、迁场封闭依据和同轮跨度全部成立才发布。冲回指定活动结算，逐队记相反分录；旧证据及其他轮次分录仍可查。积分相同时按净胜分排名，仍相同则并列。 |

`round-report` 输出材料巡查号、窗口号、迁场来源、结果修订、跨度与阻断原因。迟到证据令已结算快照出现差异时，当前轮显示 `review-required`，由值班人员提交显式冲回。`audit` 按入账顺序从事件重建临时 SQLite，比较所有投影表并返回重算轮次与联赛榜。发现投影不一致时退出码为 1，业务拒绝或无法读取返回 2。

实现集中于 `grounds.py` 的场地证据、`eligibility.py` 的分组和窗口、`schedule.py` 的轮次与占场、`ledger.py` 的比赛结果和积分。`fixtures/league.json` 演示双场同轮及一次补赛。`fixtures/late-recall.json` 加入覆盖补赛时段的裁判停用并冲回。`fixtures/rejections.json` 保存对账和拒绝案例，由测试在指定前置状态下执行。
