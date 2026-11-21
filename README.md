# SDC Briefly

The watch industry's news, collected every morning from around 90 sources and published at the public page.

## Add or remove a source
Open `sources.txt` on GitHub, press the pencil icon, edit, then press **Commit changes**.
The page rebuilds itself within a few minutes. One source per line:

    Name | address | kind

- `watch` – a watch site; every article is kept
- `general` – a broader site; only articles that mention watches are kept
- `search` – the address is a search phrase; finds watch news on any site

The address can be a site's home page; Briefly finds the feed itself.

## Fetch now
Actions tab → **Daily fetch and publish** → **Run workflow**.

## Ranking
Without an AI key, stories are ranked by keyword rules. To switch on AI ranking and translation,
add a repository secret called `ANTHROPIC_API_KEY` (Settings → Secrets and variables → Actions).
