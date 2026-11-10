import tempfile
import unittest
from pathlib import Path

from console.logs import LogReader


class LogPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'worker.log'
        self.reader = LogReader()

    def test_pages_cover_the_whole_file_beyond_the_old_tail_limit(self):
        lines = [f'{number}: ' + 'x' * 90 for number in range(1, 1501)]
        self.path.write_text('\n'.join(lines) + '\n')
        first = self.reader.read(self.path, 1, 10)
        self.assertEqual(first['content'].splitlines(), lines[:10])
        self.assertEqual((first['pages'], first['total_lines']), (150, 1500))
        for page in (13, 14, 50, 150):
            result = self.reader.read(self.path, page, 10)
            self.assertEqual(result['content'].splitlines(), lines[(page-1)*10:page*10])
        self.assertEqual(self.reader.read(self.path, 0, 10)['start_line'], 1491)
        self.assertEqual(self.reader.read(self.path, 999, 10)['page'], 150)

    def test_append_partial_line_truncate_and_replacement_refresh_the_index(self):
        self.path.write_text('first\nsecond')
        self.assertEqual(self.reader.read(self.path, 1, 10)['total_lines'], 2)
        with self.path.open('a') as output:
            output.write(' extended\nthird\n')
        self.assertEqual(self.reader.read(self.path, 1, 10)['content'], 'first\nsecond extended\nthird\n')
        self.path.write_text('short\n')
        self.assertEqual(self.reader.read(self.path, 1, 10)['total_lines'], 1)
        replacement = self.path.with_suffix('.new')
        replacement.write_text('replacement')
        replacement.replace(self.path)
        self.assertEqual(self.reader.read(self.path, 1, 10)['content'], 'replacement')

    def test_pages_preserve_trailing_blank_lines_and_can_be_joined(self):
        original = 'first\nsecond\n\n\nlast\n\n'
        self.path.write_text(original)
        pages = [self.reader.read(self.path, page, 2)['content'] for page in (1, 2, 3)]
        self.assertEqual(''.join(pages), original)

    def test_utf8_redaction_empty_and_long_lines_keep_responses_bounded(self):
        self.path.write_text('测试行\r\npassword=secret-value\n' + '界' * 90000 + '\ntail\n')
        result = self.reader.read(self.path, 1, 10)
        self.assertEqual([line['number'] for line in result['lines']], [1, 2, 3, 4])
        self.assertNotIn('secret-value', result['content'])
        self.assertIn('测试行', result['content'])
        self.assertTrue(result['lines'][2]['truncated'])
        self.assertLessEqual(len(result['content'].encode()), 65536)
        self.assertEqual(result['lines'][-1]['text'], 'tail')
        self.path.write_text('')
        self.assertEqual(self.reader.read(self.path, 1, 10)['lines'], [])
