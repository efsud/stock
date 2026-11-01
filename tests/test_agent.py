import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from stock_agent.app import AlertState, analyse, choose_destination, closed_bar, daily_due, daily_summary, deliver, deliver_daily, discovery_gate, parse_headlines, rsi, universe

NOW = datetime(2026, 11, 1, 23, tzinfo=timezone.utc)
CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text())


def series():
    bars = []
    for i in range(260):
        price = 100 + i * .1 + (i % 4) * .3
        bars.append({'t': int((NOW-timedelta(days=260-i)).timestamp()),
                     'o': price, 'h': price+1, 'l': price-1, 'c': price, 'v': 1000, 'raw': price})
    return {'symbol': 'TEST', 'name': 'Test', 'currency': 'CAD', 'crypto': False,
            'timezone': 'America/Toronto', 'bars': bars}


class SetupTests(unittest.TestCase):
    def test_empty_chat_list_can_refresh_without_reentering_token(self):
        responses = [{'ok': True, 'result': []}, {'ok': True, 'result': [
            {'message': {'chat': {'id': 12345, 'first_name': 'Test'}}}]}]
        with patch('stock_agent.app.request_json', side_effect=responses) as request, \
             patch('builtins.input', side_effect=['', '12345']), patch('builtins.print'):
            self.assertEqual(choose_destination('test-token', 'test_bot'), '12345')
            self.assertEqual(request.call_count, 2)

    def test_blank_and_invalid_chat_id_do_not_exit_setup(self):
        with patch('stock_agent.app.request_json', return_value={'ok': True, 'result': []}), \
             patch('builtins.input', side_effect=['', 'my_bot', '-12345']), patch('builtins.print'):
            self.assertEqual(choose_destination('test-token', 'test_bot'), '-12345')


class DailySummaryTests(unittest.TestCase):
    def row(self, symbol, signal, horizon='SWING', owned=True):
        return {'symbol': symbol, 'signal': signal, 'horizon': horizon, 'owned': owned}

    def test_no_action_is_one_short_sentence(self):
        self.assertEqual(daily_summary([self.row('A', 'WAIT')], {}), 'No buys or sells recommended.')

    def test_action_summary_omits_status_and_unowned_sells(self):
        text = daily_summary([self.row('A', 'BUY_CANDIDATE'), self.row('B', 'EXIT_REVIEW'),
                              self.row('C', 'WAIT'), self.row('D', 'EXIT_REVIEW', owned=False),
                              self.row('E', 'BUY_CANDIDATE', 'LONG_TERM')], {})
        self.assertEqual(text, 'Daily-price signals\nSwing: Buy A; Sell B.\nLong-term: Buy E.')

    def test_conflicts_never_recommend_buy_and_sell_same_symbol(self):
        text = daily_summary([self.row('A', 'BUY_CANDIDATE'), self.row('A', 'EXIT_REVIEW', 'LONG_TERM')], {})
        self.assertNotIn('Buy A', text)
        self.assertNotIn('Sell A', text)
        self.assertIn('Mixed signals: A.', text)

    def test_outside_ideas_limited_and_holdings_keep_priority(self):
        rows = [dict(self.row(s, 'BUY_CANDIDATE', owned=False), strength=score, atr_pct=2)
                for s, score in [('A', 85), ('B', 90), ('C', 95), ('D', 100)]]
        rows += [self.row('HOLDING', 'BUY_CANDIDATE'), self.row('SELL_HOLDING', 'EXIT_REVIEW')]
        text = daily_summary(rows, {}, max_new=2)
        self.assertIn('Buy HOLDING, C, D;', text)
        self.assertIn('Sell SELL_HOLDING', text)
        self.assertNotIn('Buy A', text)
        self.assertNotIn(', B', text)

    def test_same_outside_symbol_uses_only_one_idea_slot(self):
        rows = [dict(self.row('A', 'BUY_CANDIDATE', horizon=h, owned=False), strength=100)
                for h in ('SWING', 'LONG_TERM')]
        rows.append(dict(self.row('B', 'BUY_CANDIDATE', owned=False), strength=90))
        self.assertIn('Buy A, B', daily_summary(rows, {}, max_new=2))

    def test_missing_data_is_not_a_no_action_recommendation(self):
        text = daily_summary([self.row('A', 'DATA_STALE')], {'A': 'stale'})
        self.assertIn('Analysis unavailable', text)

    def test_noon_delivery_deduplicates_restarts_and_retries_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.db'
            state = AlertState(path)
            report = {'results': [self.row('A', 'WAIT')], 'errors': {}}
            before = datetime(2026, 11, 2, 16, 59, tzinfo=timezone.utc)
            noon = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
            sent = []
            self.assertFalse(deliver_daily(report, CONFIG, state, before, sent.append))
            def fail(_):
                raise RuntimeError('offline')
            with self.assertRaises(RuntimeError):
                deliver_daily(report, CONFIG, state, noon, fail)
            self.assertTrue(deliver_daily(report, CONFIG, state, noon, sent.append))
            state.db.close()
            state = AlertState(path)
            self.assertFalse(deliver_daily(report, CONFIG, state, noon, sent.append))
            tomorrow = noon + timedelta(days=1)
            self.assertTrue(deliver_daily(report, CONFIG, state, tomorrow, sent.append))
            self.assertEqual(len(sent), 2)
            state.db.close()

    def test_noon_stays_local_across_daylight_saving(self):
        with tempfile.TemporaryDirectory() as folder:
            state = AlertState(Path(folder)/'state.db')
            summer = datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc)
            winter = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
            self.assertTrue(daily_due(CONFIG, state, summer)[0])
            self.assertTrue(daily_due(CONFIG, state, winter)[0])
            self.assertFalse(daily_due(CONFIG, state, winter+timedelta(hours=1))[0])
            state.db.close()


class UniverseTests(unittest.TestCase):
    def test_broad_universe_keeps_priority_and_has_no_duplicate_symbols(self):
        symbols, sectors = universe(CONFIG)
        self.assertGreater(len(symbols), 250)
        self.assertGreaterEqual(len(set(sectors.values())), 11)
        self.assertEqual(symbols[:len(CONFIG['watchlist'])], CONFIG['watchlist'])
        self.assertEqual(len(symbols), len(set(symbols)))
        extra = dict(CONFIG, holdings=CONFIG['holdings']+['EXTRA_HOLDING'])
        self.assertIn('EXTRA_HOLDING', universe(extra)[0])

    def test_discovery_liquidity_gate(self):
        data = series()
        self.assertFalse(discovery_gate(data, CONFIG)[0])
        for bar in data['bars']:
            bar['v'] = 100000
        self.assertTrue(discovery_gate(data, CONFIG)[0])
        for bar in data['bars']:
            bar['raw'] = 1
        self.assertFalse(discovery_gate(data, CONFIG)[0])


class AnalysisTests(unittest.TestCase):
    def test_news_rejects_old_future_and_unsafe_links(self):
        xml = b'''<rss><channel>
        <item><title>Recent event</title><pubDate>Sun, 01 Nov 2026 20:00:00 GMT</pubDate><link>https://example.com/recent</link></item>
        <item><title>Old</title><pubDate>Sun, 01 Nov 2025 20:00:00 GMT</pubDate><link>https://example.com/old</link></item>
        <item><title>Future</title><pubDate>Mon, 02 Nov 2026 20:00:00 GMT</pubDate><link>https://example.com/future</link></item>
        <item><title>Unsafe</title><pubDate>Sun, 01 Nov 2026 20:00:00 GMT</pubDate><link>javascript:alert(1)</link></item>
        </channel></rss>'''
        self.assertEqual([r['title'] for r in parse_headlines(xml, NOW)], ['Recent event'])

    def test_rsi_flat_and_rising(self):
        self.assertEqual(rsi([10]*30), 50)
        self.assertEqual(rsi(list(range(30))), 100)

    def test_excludes_partial_equity_and_crypto_bars(self):
        stamp = int(datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc).timestamp())
        self.assertFalse(closed_bar(stamp, False, 'America/Toronto', datetime(2026, 10, 30, 19, tzinfo=timezone.utc)))
        self.assertTrue(closed_bar(stamp, False, 'America/Toronto', NOW))
        midnight = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
        self.assertFalse(closed_bar(midnight, True, 'UTC', NOW))

    def test_stale_prices_suppress_trades_and_activity(self):
        data = series()
        for bar in data['bars']:
            bar['t'] -= 10 * 86400
        result = analyse(data, None, CONFIG, NOW)
        self.assertEqual(result['signal'], 'DATA_STALE')
        self.assertFalse(result['unusual_activity'])

    def test_currency_and_horizon_preserved(self):
        result = analyse(series(), series(), CONFIG, NOW, 'LONG_TERM')
        self.assertEqual(result['currency'], 'CAD')
        self.assertEqual(result['horizon'], 'LONG_TERM')
        self.assertGreater(result['atr_pct'], 0)

    def test_missing_benchmark_never_buy(self):
        result = analyse(series(), None, CONFIG, NOW)
        self.assertNotEqual(result['signal'], 'BUY_CANDIDATE')

    def test_buy_candidate_and_volatility_filter(self):
        data, benchmark = series(), series()
        for bar in benchmark['bars']:
            bar.update(o=100, h=101, l=99, c=100, raw=100)
        for horizon in ['SWING', 'LONG_TERM']:
            result = analyse(data, benchmark, CONFIG, NOW, horizon)
            self.assertEqual(result['signal'], 'BUY_CANDIDATE')
            self.assertEqual(result['strength'], 100)
            strict = dict(CONFIG, max_buy_atr_pct=.1)
            self.assertNotEqual(analyse(data, benchmark, strict, NOW, horizon)['signal'], 'BUY_CANDIDATE')

    def test_breakdown_exits_both_horizons(self):
        data = series()
        for bar in data['bars'][-60:]:
            bar.update(o=50, h=51, l=49, c=50, raw=50)
        for horizon in ['SWING', 'LONG_TERM']:
            self.assertEqual(analyse(data, series(), CONFIG, NOW, horizon)['signal'], 'EXIT_REVIEW')


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = AlertState(Path(self.directory.name)/'state.sqlite3')
        self.row = analyse(series(), series(), CONFIG, NOW)
        self.row.update(signal='BUY_CANDIDATE', unusual_activity=False)

    def tearDown(self):
        self.state.db.close()
        self.directory.cleanup()

    def test_deduplicates_and_realerts_after_reset(self):
        sent = []
        deliver([self.row], {}, self.state, sent.append)
        deliver([self.row], {}, self.state, sent.append)
        self.assertEqual(len(sent), 1)
        waiting = dict(self.row, signal='WAIT')
        deliver([waiting], {}, self.state, sent.append)
        deliver([self.row], {}, self.state, sent.append)
        self.assertEqual(len(sent), 2)

    def test_failed_delivery_remains_retryable(self):
        def fail(_):
            raise RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            deliver([self.row], {}, self.state, fail)
        self.assertIsNone(self.state.signal('TEST:SWING'))
        sent = []
        deliver([self.row], {}, self.state, sent.append)
        self.assertEqual(len(sent), 1)

    def test_horizons_are_independent(self):
        sent = []
        other = dict(self.row, horizon='LONG_TERM')
        deliver([self.row, other], {}, self.state, sent.append)
        self.assertEqual(len(sent), 2)

    def test_stale_does_not_reset_regime(self):
        sent = []
        deliver([self.row], {}, self.state, sent.append)
        deliver([dict(self.row, signal='DATA_STALE')], {}, self.state, sent.append)
        deliver([self.row], {}, self.state, sent.append)
        self.assertEqual(len(sent), 1)

    def test_activity_sent_once_per_bar_across_horizons(self):
        sent = []
        a = dict(self.row, signal='WAIT', unusual_activity=True)
        b = dict(a, horizon='LONG_TERM')
        deliver([a, b], {}, self.state, sent.append)
        deliver([a, b], {}, self.state, sent.append)
        self.assertEqual(len(sent), 1)

    def test_news_deduplication(self):
        sent = []
        news = {'TEST': [{'title': 'Event', 'url': 'https://example.com/event', 'published': NOW.isoformat()}]}
        deliver([], {}, self.state, sent.append, news)
        deliver([], {}, self.state, sent.append, news)
        self.assertEqual(len(sent), 1)


if __name__ == '__main__':
    unittest.main()
