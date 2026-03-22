# TestCases

Use the controller script instead of calling pytest paths manually.

```bash
python TestCases/run_testcases.py --suite all --module all
python TestCases/run_testcases.py --suite smoke-release --module deployment
python TestCases/run_testcases.py --list --suite all --module tradingbot
```

Outputs are written to `Saved/TestCases/<run_id>/`.
