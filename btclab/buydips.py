import os
import time
import logging
import ccxt
from retry.api import retry, retry_call
import typer
import logging
from typing import List
import crypto
import utils
import db
from logconf import logger
from datetime import datetime, timedelta
from ccxt.base.errors import InsufficientFunds, NetworkError, RequestTimeout


config = utils.get_config()

def print_header(symbols, freq,  amount_usd, min_drop, min_additional_drop, dry_run):
    title = 'Crypto prices monitor running. Hit q to quit'
    print(f'\n{"-" * len(title)}\n{title}\n{"-" * len(title)}')
    if dry_run:
        print('Running in summulation mode\n')
    
    print(f'1) Tracking price changes in: {" ".join(symbols)} every {freq} minutes')
    print(f'2) Any drop of {min_drop}% or more will trigger a buy order of {amount_usd} [Symbol]/USDT')
    print(f'3) Any further drop of more than {min_additional_drop}% (relative to prev buy) will also be bought')
    print()


def bought_less_than_24h_ago(symbol:str, orders: dict) -> bool:
    if symbol in orders:
        now = datetime.now()
        timestamp = orders[symbol]['timestamp']
        if '.' not in str(timestamp):
            timestamp /= 1000
        bought_on = datetime.fromtimestamp(timestamp)
        diff = now - bought_on
        return diff.days <= 1
    return False


@retry((RequestTimeout, NetworkError), tries=8, delay=15, backoff=2, logger=logger)
def main(
        symbols: List[str] = typer.Argument(None, 
            help='The symbols you want to buy if they dip enough. e.g: BTC/USDT, ETH/USDC', show_default=False),
        amount_usd: float = typer.Option(config['General']['order_amount_usd'], '--amount-usd', '-a', 
            help='Amount to buy of symbol in base currency'), 
        freq: float = typer.Option(config['General']['frequency'], '--freq', '-f',
            help='Frequency in minutes to check for new price drops'),
        min_drop: float = typer.Option(config['General']['min_initial_drop'], '--min-drop', '-m', 
            help='Min drop in percentage in the last 24 hours for placing a buy order'),
        min_additional_drop: float = typer.Option(config['General']['min_additional_drop'], '--next-drop', '-n',
            help='The min additional drop in percentage to buy a symbol previoulsy bought'),
        quote_currency: str = typer.Option('USDT', help='Quote curreny to use when none is given in symbols list'),
        dry_run: bool = typer.Option(config['General']['dry_run'], 
            help='Run in simmulation mode. Don\'t buy anything'),
        reset_cache: bool = typer.Option(False, '--reset-cache', '-r', help='Reset info of previous operations'),
        verbose: bool = typer.Option(False, '--verbose', '-v', help='Verbose mode')):

    """
    Example usage:
    python buydips BTC ETH DOT --freq 10 --min-drop 7 --min-aditional-drop 2

    Start checking prices of BTC/USDT ETH/USDT and DOT/USDT every 10 minutes
    Buy the ones with a drop in the last 24h greater than 7% 
    If the biggest drop is in a symbol previouly bought, buy again only if it is down 2% from last buy price
    """

    if verbose:
        logger.setLevel(logging.DEBUG)

    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = config['IM']['telegram_chat_id']
    
    if not symbols:
        symbols = config['General']['tickers']
    
    symbols = [s.upper() for s in symbols]
    symbols = [f'{s}/{quote_currency}' if '/' not in s else s for s in symbols ]
    start_msg = 'Starting new session'
    if dry_run:
        start_msg += ' (Running in simmulation mode)'
    print()
    logger.info(start_msg)
    
    api_key = os.environ.get('BINANCE_API_KEY')
    api_secret = os.environ.get('BINANCE_API_SECRET')
    if api_key is None or api_secret is None:
        logger.warning('Add your credentials to BINANCE_API_KEY and BINANCE_API_SECRET environment variables to prevent entering on every execution')

    if api_key is None:
        api_key = typer.prompt('Enter your Binance API key')

    if api_secret is None:
        api_secret = typer.prompt('Enter your Binance API secret')

    # print_header(symbols, freq, amount_usd, min_drop, min_additional_drop, dry_run)
    binance = ccxt.binance(
        {
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        }
    )

    # Check if symbols are supported by the exchange
    non_supported_symbols = crypto.get_unsupported_symbols(binance, symbols)
    if len(non_supported_symbols) > 0:
        logging.error((f'The following symbol(s) are not supported in {binance.name}: '
                            f'{", ".join(non_supported_symbols)}. Execution stoped\n'))
        raise typer.Exit(code=-1)

    # Load previous orders
    orders = db.get_orders() if not reset_cache else {}

    logger.info(f'Tracking price drops in: {", ".join(symbols)}')
    logger.info(f'Min drop level set to {min_drop}% for the first buy')
    logger.info(f'Additional drop level of {min_additional_drop}% for symbols already bought')
    logger.info('Run with --verbose option to see more detail\n')

    if orders:
        logger.info('You previoulsy bought:')
        for key, value in orders.items():
            logger.info(f'{key} -> {value["amount"]} @ {value["price"]}')

    typer.echo()

    while True:
        tickers = binance.fetch_tickers(symbols)
        
        for symbol, ticker in tickers.items():
            buy_first_time = False
            buy_again = False
            if symbol in orders and bought_less_than_24h_ago(symbol, orders):
                discount_pct = (ticker['last'] / orders[symbol]['price'] - 1) * 100
                buy_again = discount_pct < -min_additional_drop
                
            else:
                buy_first_time = ticker['percentage'] < -min_drop
            
            if buy_first_time or buy_again:
                try:
                    order = crypto.place_order(exchange=binance, 
                                                symbol=symbol, 
                                                price=ticker['last'], 
                                                amount_in_usd=amount_usd,
                                                dry_run=dry_run)
                except InsufficientFunds:
                    retry_after = config['General']['retry_after']
                    msg = f'Insufficient funds. Trying again in {retry_after} minutes...'
                    logger.warning(msg)
                    utils.send_msg(bot_token, chat_id, msg)
                    time.sleep(retry_after * 60)
                    continue
                else:
                    orders[symbol] = order
                    db.save(orders)
                    if buy_again:
                        msg = f'Buying {symbol} @ {ticker["last"]:,}, {discount_pct:.1f}% lower than previous buy'
                    else:
                        msg = f'Buying {symbol} @ {ticker["last"]:,}, {ticker["percentage"]:.1f}% lower than 24h ago'
                    logger.info(msg)
                    utils.send_msg(bot_token, chat_id, msg)
            else:
                logger.debug(f'{symbol} currently selling at {ticker["last"]} ({ticker["percentage"]:.1f}%) - Not enough discount')

        logger.info(f'Checking again for price drops in {freq} minutes...')
        time.sleep(freq * 60)


if __name__ == '__main__':
    typer.run(main)