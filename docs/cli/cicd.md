# CI/CD Integration

Use `vt` in your pipeline to validate config files on every PR and sync on every merge.

---

## Recommended workflow

A typical CI/CD setup uses two steps:

1. **On pull request** — validate the config file against the CUE schema. Block the PR if validation fails.
2. **On merge to main** — sync the config to the production datasource.

---

## GitHub Actions

### Validate on PR + sync on merge

```yaml
name: Config sync

on:
  pull_request:
    paths:
      - "configs/**"
  push:
    branches: [main]
    paths:
      - "configs/**"

jobs:
  validate:
    name: Validate config
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    steps:
      - uses: actions/checkout@v4

      - name: Download vt
        run: |
          curl -sSL https://github.com/jarin-devoss/varTrack/releases/latest/download/vt_linux_amd64 \
            -o /usr/local/bin/vt && chmod +x /usr/local/bin/vt

      - name: Validate
        env:
          VARTRACK_SERVER: ${{ vars.VARTRACK_SERVER }}
          VARTRACK_TOKEN:  ${{ secrets.VARTRACK_TOKEN }}
        run: vt validate --file configs/app.yaml --datasource mongo

  sync:
    name: Sync to production
    runs-on: ubuntu-latest
    if: github.event_name == 'push'
    steps:
      - uses: actions/checkout@v4

      - name: Download vt
        run: |
          curl -sSL https://github.com/jarin-devoss/varTrack/releases/latest/download/vt_linux_amd64 \
            -o /usr/local/bin/vt && chmod +x /usr/local/bin/vt

      - name: Sync to production
        env:
          VARTRACK_SERVER: ${{ vars.VARTRACK_SERVER }}
          VARTRACK_TOKEN:  ${{ secrets.VARTRACK_TOKEN }}
          VARTRACK_TENANT: myapp
        run: |
          vt sync \
            --file        configs/app.yaml \
            --datasource  mongo \
            --env         production \
            --wait \
            --label       "${{ github.ref_name }}@${{ github.sha }}"
```

### Multi-environment sync

```yaml
strategy:
  matrix:
    include:
      - env: staging
        datasource: mongo-staging
      - env: production
        datasource: mongo-primary

steps:
  - name: Sync ${{ matrix.env }}
    env:
      VARTRACK_SERVER: ${{ vars.VARTRACK_SERVER }}
      VARTRACK_TOKEN:  ${{ secrets.VARTRACK_TOKEN }}
    run: |
      vt sync \
        --file       configs/app.yaml \
        --datasource ${{ matrix.datasource }} \
        --env        ${{ matrix.env }} \
        --wait
```

---

## GitLab CI

```yaml
variables:
  VARTRACK_SERVER: https://gateway.example.com

validate:config:
  stage: test
  only:
    - merge_requests
  script:
    - vt validate --file configs/app.yaml --datasource mongo

sync:staging:
  stage: deploy
  environment: staging
  only:
    - develop
  script:
    - vt sync --file configs/app.yaml --datasource mongo --env staging --wait

sync:production:
  stage: deploy
  environment: production
  only:
    - main
  script:
    - vt sync --file configs/app.yaml --datasource mongo --env production --wait
  when: manual
```

---

## Dry-run on every PR

Add a dry-run step to every PR to see exactly what would be written:

```yaml
- name: Dry-run sync
  env:
    VARTRACK_SERVER: ${{ vars.VARTRACK_SERVER }}
    VARTRACK_TOKEN:  ${{ secrets.VARTRACK_TOKEN }}
  run: |
    vt sync \
      --file        configs/app.yaml \
      --datasource  mongo \
      --env         production \
      --dry-run \
      --json
```

The JSON output shows the exact keys that would be written, changed, and deleted — making the diff visible in the PR.

---

## Using environment variables

No `vt login` needed in CI/CD — just set the environment variables:

| Variable | Where to store |
|---|---|
| `VARTRACK_SERVER` | Repository variable (`vars.VARTRACK_SERVER`) |
| `VARTRACK_TOKEN` | Repository secret (`secrets.VARTRACK_TOKEN`) |
| `VARTRACK_TENANT` | Repository variable or hardcoded in the workflow |
