# Contributing

```bash
git clone https://github.com/thesvj/gq && cd gq
uv venv && uv pip install -e '.[dev]' shellcheck-py
.venv/bin/pytest -q          # ~80 tests, no cluster needed
.venv/bin/gq demo            # dashboard on made-up data
```

- **Cluster-side code** (`gq/remote_files/`) must run on Python 3.6 and plain bash. CI checks this
  with `vermin` and `shellcheck`. It runs on login nodes, so keep it to read-only queries.
- **New site behaviour?** Add an anonymised fixture under `tests/fixtures/<site>/`
  (`scontrol show partition -o`, `scontrol show node -o`, `sacctmgr -nP show qos format=…`,
  `sacctmgr -nP show assoc user=$USER format=…`) and a test. Replace real user, account, host and node
  names and IPs before committing. `tests/test_no_secrets.py` catches the obvious ones, and you can list
  your own site words in a file named by `$GQ_DENYLIST`.
- Screenshots come from `gq demo` only, never from a real cluster.
