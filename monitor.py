from __future__ import annotations

import argparse
import json
import logging
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_CONFIG_PATH = BASE_DIR / "config.example.json"
STATE_PATH = BASE_DIR / "state.json"
BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    close: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Binance USD-M 15-minute candles for EMA20 side changes."
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Configuration file. Defaults to config.json, then config.example.json.",
    )
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log alerts without sending email. State is still updated.",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a test email and exit.",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="Check only the latest closed-candle pattern without state.json.",
    )
    return parser.parse_args()


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        path = DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH

    with path.open("r", encoding="utf-8") as file:
        config = json.load(file)

    required = {
        "symbols",
        "interval",
        "ema_period",
        "confirmation_candles",
        "poll_seconds",
        "history_limit",
        "email",
    }
    missing = sorted(required - config.keys())
    if missing:
        raise ValueError(f"配置缺少字段: {', '.join(missing)}")

    if not config["symbols"]:
        raise ValueError("symbols 不能为空")
    if config["ema_period"] < 2:
        raise ValueError("ema_period 必须大于等于 2")
    if config["confirmation_candles"] < 1:
        raise ValueError("confirmation_candles 必须大于等于 1")
    if not 20 <= config["history_limit"] <= 1500:
        raise ValueError("history_limit 必须在 20 到 1500 之间")

    email_config = config["email"]
    if not isinstance(email_config, dict):
        raise ValueError("email 必须是一个配置对象")
    required_email_fields = {
        "smtp_host",
        "smtp_port",
        "sender",
        "auth_code",
        "recipients",
    }
    missing_email_fields = sorted(required_email_fields - email_config.keys())
    if missing_email_fields:
        raise ValueError(f"email 配置缺少字段: {', '.join(missing_email_fields)}")
    if not isinstance(email_config["recipients"], list):
        raise ValueError("email.recipients 必须是邮箱地址列表")

    config["symbols"] = [str(symbol).upper() for symbol in config["symbols"]]
    return config


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 1, "symbols": {}}

    with STATE_PATH.open("r", encoding="utf-8") as file:
        state = json.load(file)
    state.setdefault("version", 1)
    state.setdefault("symbols", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    temporary_path = STATE_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary_path.replace(STATE_PATH)


def fetch_closed_candles(symbol: str, interval: str, limit: int) -> list[Candle]:
    query = urllib.parse.urlencode(
        {"symbol": symbol, "interval": interval, "limit": limit}
    )
    request = urllib.request.Request(
        f"{BINANCE_KLINES_URL}?{query}",
        headers={"User-Agent": "binance-ema-alert/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            rows = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"币安接口返回 HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"无法连接币安接口: {error.reason}") from error

    now_ms = int(time.time() * 1000)
    return [
        Candle(
            open_time=int(row[0]),
            close_time=int(row[6]),
            close=float(row[4]),
        )
        for row in rows
        if int(row[6]) < now_ms
    ]


def calculate_ema(closes: list[float], period: int) -> list[float]:
    if not closes:
        return []

    alpha = 2.0 / (period + 1.0)
    values = [closes[0]]
    for close in closes[1:]:
        values.append(alpha * close + (1.0 - alpha) * values[-1])
    return values


def candle_side(close: float, ema: float, epsilon: float = 1e-12) -> str:
    if close > ema + epsilon:
        return "above"
    if close < ema - epsilon:
        return "below"
    return "equal"


def initialize_symbol_state(
    candles: list[Candle], ema_values: list[float]
) -> dict[str, Any]:
    confirmed_side = "equal"
    for candle, ema in reversed(list(zip(candles, ema_values))):
        confirmed_side = candle_side(candle.close, ema)
        if confirmed_side != "equal":
            break

    if confirmed_side == "equal":
        raise RuntimeError("无法从历史K线确定EMA20所在侧")

    return {
        "confirmed_side": confirmed_side,
        "pending_side": None,
        "consecutive_count": 0,
        "last_open_time": candles[-1].open_time,
    }


def advance_state(
    symbol_state: dict[str, Any],
    current_side: str,
    confirmation_candles: int,
) -> tuple[str, str] | None:
    confirmed_side = symbol_state["confirmed_side"]

    if current_side == "equal":
        symbol_state["pending_side"] = None
        symbol_state["consecutive_count"] = 0
        return None

    if current_side == confirmed_side:
        symbol_state["pending_side"] = None
        symbol_state["consecutive_count"] = 0
        return None

    if current_side == symbol_state.get("pending_side"):
        symbol_state["consecutive_count"] += 1
    else:
        symbol_state["pending_side"] = current_side
        symbol_state["consecutive_count"] = 1

    if symbol_state["consecutive_count"] < confirmation_candles:
        return None

    previous_side = confirmed_side
    symbol_state["confirmed_side"] = current_side
    symbol_state["pending_side"] = None
    symbol_state["consecutive_count"] = 0
    return previous_side, current_side


def detect_latest_transition(
    candles: list[Candle],
    ema_values: list[float],
    confirmation_candles: int,
) -> tuple[str, str] | None:
    required_candles = confirmation_candles + 1
    if len(candles) < required_candles:
        return None

    recent_pairs = list(zip(candles, ema_values))[-required_candles:]
    sides = [candle_side(candle.close, ema) for candle, ema in recent_pairs]
    previous_side = sides[0]
    confirmed_sides = sides[1:]

    if previous_side == "equal":
        return None
    current_side = confirmed_sides[0]
    if current_side == "equal" or current_side == previous_side:
        return None
    if not all(side == current_side for side in confirmed_sides):
        return None
    return previous_side, current_side


def format_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def build_alert_message(
    symbol: str,
    previous_side: str,
    current_side: str,
    candle: Candle,
    close: float,
    ema: float,
    confirmation_candles: int,
) -> tuple[str, str]:
    direction = "向上" if current_side == "above" else "向下"
    side_text = "上方" if current_side == "above" else "下方"
    previous_text = "上方" if previous_side == "above" else "下方"
    close_time = datetime.fromtimestamp(
        candle.close_time / 1000, tz=CHINA_TIMEZONE
    ).strftime("%Y-%m-%d %H:%M:%S")

    subject = f"[币安提醒] {symbol} 15分钟EMA20{direction}确认"
    body = (
        "币安 U 本位合约 EMA20 提醒\n\n"
        f"交易对：{symbol}\n"
        f"周期：15分钟\n"
        f"方向：由 EMA20 {previous_text}转到{side_text}\n"
        f"确认：连续 {confirmation_candles} 根已收盘K线位于EMA20{side_text}\n"
        f"确认K线收盘价：{format_price(close)}\n"
        f"确认K线EMA20：{format_price(ema)}\n"
        f"确认K线收盘时间：{close_time}（北京时间）\n"
    )
    return subject, body


def send_email(subject: str, body: str, email_config: dict[str, Any]) -> None:
    smtp_host = str(email_config["smtp_host"]).strip()
    smtp_port = int(email_config["smtp_port"])
    smtp_user = str(email_config["sender"]).strip()
    auth_code = str(email_config["auth_code"]).strip()
    recipients = [
        str(item).strip() for item in email_config["recipients"] if str(item).strip()
    ]

    if not smtp_host:
        raise RuntimeError("config.json 中未填写 email.smtp_host")
    if not smtp_user:
        raise RuntimeError("config.json 中未填写 email.sender")
    if not auth_code:
        raise RuntimeError("config.json 中未填写 email.auth_code")
    if not recipients:
        raise RuntimeError("config.json 中未填写 email.recipients")

    message = EmailMessage()
    message["From"] = smtp_user
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        smtp_host, smtp_port, context=context, timeout=20
    ) as smtp:
        smtp.login(smtp_user, auth_code)
        smtp.send_message(message)


def process_symbol(
    symbol: str,
    config: dict[str, Any],
    state: dict[str, Any],
    dry_run: bool,
) -> None:
    candles = fetch_closed_candles(
        symbol=symbol,
        interval=config["interval"],
        limit=config["history_limit"],
    )
    if len(candles) < config["ema_period"]:
        raise RuntimeError(
            f"{symbol} 已收盘K线不足: {len(candles)} < {config['ema_period']}"
        )

    ema_values = calculate_ema(
        [candle.close for candle in candles], config["ema_period"]
    )
    stored_state = state["symbols"].get(symbol)
    if stored_state is None:
        state["symbols"][symbol] = initialize_symbol_state(candles, ema_values)
        save_state(state)
        side = state["symbols"][symbol]["confirmed_side"]
        logging.info("%s 初始化完成，当前位于 EMA20 %s", symbol, side)
        return

    new_items = [
        (candle, ema)
        for candle, ema in zip(candles, ema_values)
        if candle.open_time > stored_state["last_open_time"]
    ]
    if not new_items:
        logging.debug("%s 暂无新的已收盘K线", symbol)
        return

    for candle, ema in new_items:
        candidate_state = dict(state["symbols"][symbol])
        side = candle_side(candle.close, ema)
        event = advance_state(
            candidate_state,
            current_side=side,
            confirmation_candles=config["confirmation_candles"],
        )
        candidate_state["last_open_time"] = candle.open_time

        if event is not None:
            subject, body = build_alert_message(
                symbol=symbol,
                previous_side=event[0],
                current_side=event[1],
                candle=candle,
                close=candle.close,
                ema=ema,
                confirmation_candles=config["confirmation_candles"],
            )
            if dry_run:
                logging.warning("模拟提醒：%s\n%s", subject, body)
            else:
                send_email(subject, body, config["email"])
                logging.warning("提醒邮件已发送：%s", subject)

        state["symbols"][symbol] = candidate_state
        save_state(state)
        logging.info(
            "%s 已处理 %s，收盘=%s，EMA20=%s，侧=%s，确认计数=%s",
            symbol,
            datetime.fromtimestamp(candle.close_time / 1000, tz=CHINA_TIMEZONE),
            format_price(candle.close),
            format_price(ema),
            side,
            candidate_state["consecutive_count"],
        )


def process_symbol_stateless(
    symbol: str,
    config: dict[str, Any],
    dry_run: bool,
) -> None:
    candles = fetch_closed_candles(
        symbol=symbol,
        interval=config["interval"],
        limit=config["history_limit"],
    )
    if len(candles) < config["ema_period"]:
        raise RuntimeError(
            f"{symbol} 已收盘K线不足: {len(candles)} < {config['ema_period']}"
        )

    ema_values = calculate_ema(
        [candle.close for candle in candles], config["ema_period"]
    )
    event = detect_latest_transition(
        candles,
        ema_values,
        config["confirmation_candles"],
    )
    latest_candle = candles[-1]
    latest_ema = ema_values[-1]

    if event is None:
        logging.info(
            "%s 最新收盘=%s，EMA20=%s，未触发转换提醒",
            symbol,
            format_price(latest_candle.close),
            format_price(latest_ema),
        )
        return

    subject, body = build_alert_message(
        symbol=symbol,
        previous_side=event[0],
        current_side=event[1],
        candle=latest_candle,
        close=latest_candle.close,
        ema=latest_ema,
        confirmation_candles=config["confirmation_candles"],
    )
    if dry_run:
        logging.warning("模拟提醒：%s\n%s", subject, body)
    else:
        send_email(subject, body, config["email"])
        logging.warning("提醒邮件已发送：%s", subject)


def run_check(
    config: dict[str, Any],
    state: dict[str, Any],
    dry_run: bool,
    stateless: bool = False,
) -> bool:
    succeeded = True
    for symbol in config["symbols"]:
        try:
            if stateless:
                process_symbol_stateless(symbol, config, dry_run)
            else:
                process_symbol(symbol, config, state, dry_run)
        except Exception:
            succeeded = False
            logging.exception("处理 %s 失败", symbol)
    return succeeded


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = load_config(args.config)
        if args.test_email:
            send_email(
                "[币安提醒] QQ邮箱发送测试",
                "测试成功。币安 U 本位合约 EMA20 监控程序可以发送邮件。\n",
                config["email"],
            )
            logging.info("测试邮件已发送")
            return 0

        state = {"version": 1, "symbols": {}} if args.stateless else load_state()
        while True:
            succeeded = run_check(
                config,
                state,
                args.dry_run,
                stateless=args.stateless,
            )
            if args.once:
                return 0 if succeeded else 1
            time.sleep(config["poll_seconds"])
    except KeyboardInterrupt:
        logging.info("监控已停止")
        return 0
    except Exception:
        logging.exception("程序启动失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
