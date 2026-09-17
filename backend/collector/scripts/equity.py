import math

PARENT_KEYS = ('归属于母公司股东权益合计', '归属于母公司股东的权益',
               '归属于母公司股东权益', '归属于母公司所有者权益合计')
TOTAL_KEYS = ('所有者权益(或股东权益)合计', '总权益', '股东权益合计')
MINORITY_KEYS = ('少数股东权益', '非控股股东权益')


def equity_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def equity_of(row):
    for key in PARENT_KEYS + TOTAL_KEYS:
        value = equity_number((row or {}).get(key))
        if value is not None:
            return value
    return None


def equity_close(a, b):
    return abs(a - b) <= max(1.0, abs(a) * 1e-8, abs(b) * 1e-8)


def _agreed(row, keys):
    values = [equity_number(row.get(key)) for key in keys]
    values = [v for v in values if v is not None]
    if any(not equity_close(values[0], v) for v in values[1:]):
        return None, False
    return (values[0] if values else None), True


def hk_equity_patch(row, legacy=False):
    parent_key, total_key = PARENT_KEYS[0], TOTAL_KEYS[0]
    total, total_ok = _agreed(row, TOTAL_KEYS[1:])
    minority, minority_ok = _agreed(row, MINORITY_KEYS)
    explicit, explicit_ok = _agreed(row, PARENT_KEYS[1:])
    if not (total_ok and minority_ok and explicit_ok):
        return {}, 'conflict_alias'
    assets = equity_number(row.get('资产总计'))
    liabilities = equity_number(row.get('负债合计'))
    if any(v is None for v in (total, minority, assets, liabilities)):
        return {}, 'incomplete'
    if not equity_close(assets - liabilities, total):
        return {}, 'conflict_balance'
    old_parent = equity_number(row.get(parent_key))
    old_total = equity_number(row.get(total_key))
    if legacy:
        if old_parent is None or old_total is None:
            return {}, 'incomplete'
        if equity_close(old_total, total) and equity_close(old_parent + minority, total):
            raw_parent = equity_number(row.get('股东权益'))
            if any(v is not None and not equity_close(v, old_parent) for v in (explicit, raw_parent)):
                return {}, 'conflict_parent'
            return {}, 'unchanged'
        # 只逆转旧采集器的双扣痕迹，不能把任何不平衡报表都修成资产减负债。
        if not (equity_close(old_parent, old_total - minority)
                and equity_close(old_total + minority, total)):
            return {}, 'conflict_legacy'
        parent = old_total
        raw_parent = equity_number(row.get('股东权益'))
        if raw_parent is not None and not equity_close(raw_parent, parent):
            return {}, 'conflict_parent'
    else:
        parent = equity_number(row.get('股东权益'))
        if parent is None:
            return {}, 'incomplete'
        if not equity_close(parent + minority, total):
            return {}, 'conflict_parent'
        if ((old_parent is not None and not equity_close(old_parent, parent))
                or (old_total is not None and not equity_close(old_total, total))):
            return {}, 'conflict_existing'
    if explicit is not None and not equity_close(explicit, parent):
        return {}, 'conflict_parent'
    patch = {k: v for k, v in ((parent_key, parent), (total_key, total)) if row.get(k) != v}
    return patch, 'repaired' if patch else 'unchanged'
