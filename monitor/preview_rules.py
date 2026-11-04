"""Preview a synthetic text rule match, without source reads or delivery."""
import argparse
import json
from matcher import extract

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--text',required=True,help='Use synthetic text; no real chat archive is needed.')
    args=parser.parse_args()
    try:
        result=extract(args.text)
        print(json.dumps({'matched':bool(result),'alert':result.data() if result else None},ensure_ascii=True))
    except (OSError,ValueError):
        print(json.dumps({'error':'check_local_settings_and_rules'}))
        raise SystemExit(2)

if __name__=='__main__':
    main()
