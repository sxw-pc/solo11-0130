import argparse
import json
import tempfile
from pathlib import Path

from . import operations, report, alignment, rights, privacy_scan, privacy_fixture

COMMANDS = ['track-import', 'review-decide', 'delivery-evaluate', 'alignment-evaluate',
            'rights-register', 'rights-revoke', 'privacy-scan', 'privacy-fixture']


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg = sub.add_parser(command)
        arg.add_argument('--db', required=True)
        arg.add_argument('file')
    arg = sub.add_parser('report')
    arg.add_argument('--db', required=True)
    arg.add_argument('--out', required=True)
    arg = sub.add_parser('privacy-review')
    arg.add_argument('--db', required=True)
    arg.add_argument('--audience', choices=['internal', 'external'])
    arg.add_argument('file', nargs='?')
    arg = sub.add_parser('privacy-export')
    arg.add_argument('--db', required=True)
    arg.add_argument('--out', required=True)
    arg.add_argument('--audience', default='external', choices=['internal', 'external'])
    arg.add_argument('--project')
    arg.add_argument('--vendor')
    x = parser.parse_args()
    if x.command == 'demo':
        root = Path(tempfile.mkdtemp(prefix=__package__ + '-'))
        result = operations.demo(root)
        print(json.dumps(result, ensure_ascii=False))
        print(report.build(root / 'ledger.db', root / 'report.json'))
    elif x.command == 'report':
        print(report.build(x.db, x.out))
    elif x.command == 'privacy-review':
        # Local reviewer view: raw and redacted fragments.  Never emit this
        # outside the authorized review environment.
        print(json.dumps(report.review(x.db, x.audience), ensure_ascii=False, indent=2))
    elif x.command == 'privacy-export':
        print(privacy_fixture.export(x.db, x.out, x.audience, x.project, x.vendor))
    else:
        fn = [operations.first, operations.second, operations.third, alignment.evaluate,
              rights.register, rights.revoke, privacy_scan.scan,
              privacy_fixture.build][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db, x.file), ensure_ascii=False))


if __name__ == '__main__':
    main()
