import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from transcript_utils import parse_srt, parse_vtt, parse_json_captions, timestamp_url, write_cues
from video_summary import platform_of, preferred_track


class VideoSummaryTests(unittest.TestCase):
    def test_srt(self):
        s = '1\n00:00:01,000 --> 00:00:02,200\nHello!\n\n2\n00:01:05,100 --> 00:01:06,000\nWorld.\n'
        cues = parse_srt(s)
        self.assertEqual([c['text'] for c in cues], ['Hello!', 'World.'])
        self.assertEqual(cues[1]['start'], 65.1)

    def test_vtt_cue_ids_tags(self):
        s = 'WEBVTT\n\nabc-1\n00:00:01.000 --> 00:00:02.200 align:start\n<c.colorE5>Hi</c> <i>there</i>!\n'
        cues = parse_vtt(s)
        self.assertEqual(cues[0]['text'], 'Hi there!')

    def test_bilibili_json(self):
        s = json.dumps({'body':[{'from':0.5,'to':2.1,'content':'你好'}, {'from':2.2,'to':3,'content':'世界'}]})
        self.assertEqual(len(parse_json_captions(s)), 2)

    def test_youtube_json3(self):
        s = json.dumps({'events':[{'tStartMs':1200, 'dDurationMs':2000, 'segs':[{'utf8':'hello '},{'utf8':'world'}]}, {'tStartMs':3000}]})
        cues = parse_json_captions(s)
        self.assertEqual(cues[0]['text'], 'hello world')
        self.assertEqual(len(cues), 1)

    def test_duplicates_merged(self):
        s = '1\n00:00:00,000 --> 00:00:02,000\nHi\n\n2\n00:00:01,500 --> 00:00:03,000\nHi'
        self.assertEqual(len(parse_srt(s)), 1)

    def test_timestamp_url(self):
        self.assertIn('v=x', timestamp_url('https://www.youtube.com/watch?v=x', 135))
        self.assertIn('t=135s', timestamp_url('https://youtu.be/abc', 135))
        self.assertIn('t=75', timestamp_url('https://www.bilibili.com/video/BV123/?p=2', 75))
        self.assertIn('p=2', timestamp_url('https://www.bilibili.com/video/BV123/?p=2', 75))

    def test_selection(self):
        a = {'language':'en','subtitles':{'fr':[{'ext':'vtt'}]},'automatic_captions':{'en':[{'ext':'vtt'}]}}
        self.assertEqual(preferred_track(a,'youtube'), ('en','auto'))
        b = {'subtitles':{'ai-zh':[{'ext':'json'}]},'automatic_captions':{}}
        self.assertEqual(preferred_track(b,'bilibili'), ('ai-zh','manual'))

    def test_platform_host_validation(self):
        self.assertEqual(platform_of('https://youtu.be/abc'),'youtube')
        self.assertEqual(platform_of('https://www.bilibili.com/video/BVx'),'bilibili')
        with self.assertRaises(ValueError):
            platform_of('https://youtube.com.evil.test/watch?v=abc')

    def test_chunk_output(self):
        cues = [{'start': i, 'end': i+1, 'text': f'Line {i}'} for i in range(25)]
        with tempfile.TemporaryDirectory() as d:
            count = write_cues(cues, 'https://www.youtube.com/watch?v=abc', Path(d), max_chunk_chars=180)
            self.assertGreater(count, 1)
            self.assertEqual(len(list((Path(d) / 'chunks').glob('chunk-*.md'))), count)
            self.assertEqual(len(json.loads((Path(d) / 'transcript.json').read_text())), 25)


if __name__ == '__main__':
    unittest.main()
