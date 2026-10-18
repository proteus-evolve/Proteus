"""Run the installed domain report CLI on a previously saved real Docker study."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def check(source):
    with tempfile.TemporaryDirectory() as temporary:
        output=Path(temporary)
        for private in (False,True):
            target=output/('private.json' if private else 'public.json')
            command=[sys.executable,'-m','proteus.benchmarks.coral_circle_packing.report',str(source),'--out',str(target)]
            if private:
                command.append('--include-private')
            subprocess.run(command,capture_output=True,text=True,check=True)
            report=json.loads(target.read_text())
            assert report['geometry'] and report['report_hash']
            hidden=[row for row in report['geometry'] if row['visibility']=='hidden']
            assert hidden and all((row['score'] is not None)==private for row in hidden)
            assert not private or all(row['status']=='regraded' for row in report['geometry'])
        print('Installed circle-packing report independently regraded saved real Docker geometry and preserved private scores.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    check(parser.parse_args().run)
