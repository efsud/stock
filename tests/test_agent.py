import copy
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from stock_agent.app import AlertState, analyse, closed_bar, deliver, parse_headlines, rsi

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
