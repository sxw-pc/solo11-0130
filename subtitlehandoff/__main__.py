import argparse,json,tempfile
from pathlib import Path
from . import operations,report,alignment,rights,plugin_inventory,project_open
COMMANDS=['track-import', 'review-decide', 'delivery-evaluate','alignment-evaluate','rights-register','rights-revoke','plugin-import','project-open']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
        if command=='project-open':arg.add_argument('--station')
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    for command in ('station-register','package-import'):
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('plan-solve');arg.add_argument('--db',required=True)
    arg.add_argument('--station',required=True);arg.add_argument('--projects')
    arg=sub.add_parser('plan-apply');arg.add_argument('--db',required=True)
    arg.add_argument('plan_id');arg.add_argument('--fail-step',type=int)
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='station-register':
        print(json.dumps(plugin_inventory.register_station(x.db,x.file),ensure_ascii=False))
    elif x.command=='package-import':
        print(json.dumps(plugin_inventory.import_packages(x.db,x.file),ensure_ascii=False))
    elif x.command=='plan-solve':
        ids=x.projects.split(',') if x.projects else None
        print(json.dumps(project_open.solve_plan(x.db,x.station,ids),ensure_ascii=False))
    elif x.command=='plan-apply':
        print(json.dumps(plugin_inventory.apply_plan(x.db,x.plan_id,x.fail_step),ensure_ascii=False))
    elif x.command=='project-open':
        print(json.dumps(project_open.check(x.db,x.file,x.station),ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,alignment.evaluate,rights.register,
            rights.revoke,plugin_inventory.import_inventory,project_open.check][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
