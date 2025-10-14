# Hen_Watch

Watch multiple authors on E-Hentai; notify **only when new items appear**. First run is **silent** (baseline only).

## Quick Start (local)

```bash
pip install -r requirements.txt
cp config.example.toml config.toml  # fill [telegram] if you want Telegram
export SEARCH_AUTHORS='unitynay，aurora_s2，paperhero，bride，at-sa-kon，misaaki，Ichinose Riko，Celestine，BMS07，狗百代，kala ai，kiora atumi，ドゥドゥ，halflove，FSZ，huuoliv，naks，oki senpai，Latex MBA'
export RESULT_SELECTOR='a[href*="/g/"]'
python -m hen_watch.cli --config config.toml
```

## State
A `state.json` snapshot is kept in repo root.
