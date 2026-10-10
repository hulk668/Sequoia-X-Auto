"""Sequoia-X V2 主程序入口。

运行模式：
  python main.py                    # 日常模式：增量补数据 + 跑策略 + PushPlus 汇总推送
  python main.py --prefer-akshare   # 同上，但数据更新直接走 akshare（不探测 baostock）
  python main.py --skip-sync        # 完全不更新数据，直接用库中现有数据选股
  python main.py --backfill         # 补数模式：东财日K拉全市场历史（首次/补池子用）
  python main.py --backfill --limit 200   # 补数试跑：只补前 200 只
  python main.py --backfill-baostock      # 补数（旧通道）：走 baostock，不推荐

数据源选择：baostock 免费服务长期不稳定，`PREFER_AKSHARE=true`（或 --prefer-akshare）
可跳过探测、直接用 akshare（东财）更新，并用「锚点 + 比例换算」对齐库中的后复权基准。
补数同理：baostock 的 `query_history_k_data_plus` 实测会卡死不返回，
所以 --backfill 默认改走东财日K接口（见 sequoia_x/data/backfill.py）。
"""

import argparse
import sys
from dotenv import load_dotenv
load_dotenv()

import socket
socket.setdefaulttimeout(10.0)

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.backfill import MarketBackfiller, load_symbols
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify import build_notifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy

# 名称表覆盖率低于该值时，才去 baostock 刷新一次（覆盖率够高就别打扰它）
_NAME_REFRESH_THRESHOLD = 0.95


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="补数模式：通过东财日K接口拉全市场历史（可中断续跑）",
    )
    parser.add_argument(
        "--backfill-baostock",
        action="store_true",
        help="补数模式（旧通道）：走 baostock，接口不稳，仅在东财不可用时使用",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="配合 --backfill：只补前 N 只，用于试跑",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=None,
        help="配合 --backfill：每只票保留最近 N 根 K 线（默认 400，0 表示不截断）",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "em", "qq"],
        default="auto",
        help="配合 --backfill：数据源，auto=东财优先腾讯兜底（默认）",
    )
    parser.add_argument(
        "--prefer-akshare",
        action="store_true",
        help="数据更新直接走 akshare，跳过 baostock 探测（等价于 PREFER_AKSHARE=true）",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="完全不更新数据（baostock 与 akshare 都不跑），直接用库中现有数据选股",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 命令行开关优先于配置文件：只作用于本次运行，不用改配置
        if args.prefer_akshare and not settings.prefer_akshare:
            logger.info("命令行指定 --prefer-akshare，本次改用 akshare 更新数据")
            object.__setattr__(settings, "prefer_akshare", True)

        if args.skip_sync and not settings.skip_sync:
            logger.warning("命令行指定 --skip-sync，本次完全不更新数据")
            object.__setattr__(settings, "skip_sync", True)

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill or args.backfill_baostock:
            # ── 补数模式：拉全市场历史 K 线，可中断续跑 ──
            logger.info("进入补数模式...")
            all_symbols = load_symbols(engine)
            if args.limit:
                all_symbols = all_symbols[: args.limit]
                logger.info(f"--limit 生效，只补前 {len(all_symbols)} 只（试跑）")

            if args.backfill_baostock:
                logger.warning("使用旧通道 baostock 补数（接口不稳定，卡死时直接 Ctrl+C 重跑）")
                engine.backfill(all_symbols)
            else:
                backfiller = MarketBackfiller(
                    engine, settings,
                    bars=400 if args.bars is None else args.bars,
                    source=args.source,
                )
                stats = backfiller.run(all_symbols)
                logger.info(f"补数结果：{stats}")

            logger.info(f"数据库数据截止日期：{engine.get_latest_date()}")
            logger.info("Sequoia-X V2 补数模式运行完成")
            return

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        if settings.skip_sync:
            logger.warning(
                "SKIP_SYNC 已开启，完全不更新数据"
                "（baostock、akshare、名称刷新都不跑），直接基于库中现有数据选股"
            )
        else:
            source = "akshare" if settings.prefer_akshare else "baostock（不可用则回退 akshare）"
            logger.info(f"开始拉取最新快照，数据源：{source} ...")
            count = engine.sync_today_bulk()
            logger.info(f"快照同步完成，写入 {count} 条数据")

            # 刷新股票名称（best-effort）：名称存本地 stock_name 表并随数据库缓存持久化，
            # 覆盖率已经够高就不再打扰 baostock，失败只告警、不影响流程。
            coverage = engine.name_coverage()
            if coverage >= _NAME_REFRESH_THRESHOLD:
                logger.info(f"股票名称覆盖 {coverage:.0%}，跳过刷新")
            else:
                logger.info(f"股票名称覆盖仅 {coverage:.0%}，尝试刷新...")
                try:
                    engine.refresh_stock_names()
                except Exception as exc:
                    logger.warning(f"股票名称刷新失败，沿用已有名称：{exc}")

        # 数据截止日期：同步失败或跳过时会早于今天，选股结果基于该日期及之前的数据
        data_date = engine.get_latest_date()
        logger.info(f"数据库数据截止日期：{data_date}")

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
        ]

        # 通知渠道由配置决定（notify_channel）：邮件 / PushPlus / 两者
        notifier = build_notifier(settings)

        # 5. 先把所有策略跑完并汇总，最后统一推送（不逐个策略单独发消息）
        results: dict[str, tuple[list[str], str]] = {}
        picked: list[str] = []  # 本轮选中的全部代码（去重），用于反查板块
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            # 单个策略出错不该拖垮整轮推送：兜住异常，按「本轮无选股结果」处理。
            # 否则一个 KeyError（上游接口改字段）或网络异常会一路冒到最外层
            # except → sys.exit(1)，其它策略已经算出来的结果也一起丢掉。
            try:
                selected: list[str] = strategy.run()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"{strategy_name} 执行失败，本轮跳过该策略：{exc}")
                selected = []

            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")
            results[strategy_name] = (selected, strategy.webhook_key)

            for code in selected:
                if code not in picked:
                    picked.append(code)

        # 6. 板块（所属行业）：只反查本轮选中的股票，带 30 天本地缓存
        boards: dict[str, str] = {}
        if picked:
            try:
                boards = engine.get_boards(picked)
            except Exception as exc:
                logger.warning(f"板块信息获取失败，本次推送不含板块：{exc}")

        # 7. 汇总推送：同一推送目标上的多个策略合并为一条消息
        #    名称取自本地 stock_name 表，避免每次推送都依赖 baostock 查询
        notifier.send_digest(
            results,
            engine.get_stock_names(),
            data_date=data_date,
            boards=boards,
        )

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
