"""Apply the settings and upload keys the boot partition's quartermaster.txt handed over, as the service user.

The root-side boot step writes a JSON file with "settings" and "accounts". Each key in it wins over what is saved;
a key that is missing is left alone. A bad key or value is reported by name and skipped, never the secret itself.
"""

import json
from pathlib import Path

from . import accounts as accounts_module, config as config_module


def _match(choices, raw):
    text = str(raw).strip().casefold()
    return next(((value, label) for value, label in choices
                 if text in (str(value).casefold(), label.casefold())), None)


def apply(path, config=config_module, accounts=accounts_module):
    """Returns (applied, problems): the keys that changed and one line per key that was refused."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], ["cannot read %s: %s" % (path, exc.__class__.__name__)]
    if not isinstance(data, dict):
        return [], ["%s is not a settings file" % path]
    applied, problems = [], []
    allowed = {key: choices or [] for key, _, choices, _ in config.SETTINGS}
    cfg = config.load(config.PATH)
    for key, raw in sorted((data.get("settings") or {}).items()):
        choices = allowed.get(key)
        if not choices:
            problems.append("%s is not a setting that quartermaster.txt can change" % key)
            continue
        found = _match(choices, raw)
        if found is None:
            problems.append("%s has a value that is not one of its choices" % key)
            continue
        cfg[key] = found[0]
        if key == "adsb":
            cfg["adsb_set"] = True
        if key == "country":
            cfg["country_set"] = True
        applied.append(key)
    if applied:
        config.save(cfg, config.PATH)
    keys = {field for field, _, _ in accounts.FIELDS} | {"wigle_encoded"}
    changes = {}
    for key, raw in sorted((data.get("accounts") or {}).items()):
        if key not in keys:
            problems.append("%s is not an upload key" % key)
        elif accounts.clean(raw):
            changes[key] = raw
    if changes:
        accounts.update(changes, accounts.PATH)
        applied.extend(changes)
    return applied, problems
