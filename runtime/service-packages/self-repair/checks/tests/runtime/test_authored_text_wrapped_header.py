"""Gmail wraps the 'On ... <addr> wrote:' header; the quoted history must not count as authored text."""
import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'gtm'))
from gtm.calendar.confirmation import authored_text


class T(unittest.TestCase):
    def test_wrapped_english_header_ends_authored_text(self):
        body = 'Ja, passt.\n\nOn Tue, Nov 17, 2026 at 10:00 AM Max Muster\n<max@example.invalid> wrote:\n> Dienstag 10:00 oder 11:00?\n'
        self.assertEqual(authored_text(body), 'Ja, passt.')

    def test_wrapped_german_header_ends_authored_text(self):
        body = 'Gerne 11:00.\nAm Di., 17. Nov. 2026 um 10:00 Uhr schrieb Max Muster\n<max@example.invalid>:\n> alt\n'
        self.assertNotIn('alt', authored_text('Gerne 11:00.\nAm Di., 17. Nov. 2026 um 10:00 Uhr schrieb Max Muster <max@example.invalid>:\n> alt'))

    def test_body_prose_with_wrote_but_no_address_is_not_a_boundary(self):
        body = 'Wie du schon on Monday wrote:\nich komme um 10:00.\n'
        self.assertIn('ich komme um 10:00', authored_text(body))

    def test_single_line_boundaries_still_work(self):
        self.assertEqual(authored_text('Ja.\n> zitiert'), 'Ja.')
        self.assertEqual(authored_text('Ja.\nOn Mon, X wrote:\nalt'), 'Ja.')


if __name__ == '__main__':
    unittest.main()
