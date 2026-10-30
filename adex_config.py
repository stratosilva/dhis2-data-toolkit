"""
Build an aggregate data exchange config from the instance that will run it.

The exported JSON configs these runs used to read were maintained by hand and
drifted from the servers. The sandbox 1 one carried the HTN requests only, so
running from it left every DM aggregate empty and the diabetes half of the
dashboard blank, with nothing in the output to say so. The instances define
the exchanges correctly themselves, including the facility level for their own
hierarchy, so the config is read from there instead and cannot fall behind.

Two exchanges are normally defined - the current period and the previous
twelve months - and both are needed to fill a dashboard. Their requests are
merged into the single config replace_adex.py expects.

    from adex_config import build
    config, dropped = build(api)
"""

from logzero import logger

# Where a program indicator records the aggregate data element it feeds, when
# a request does not name the attribute itself.
DEFAULT_MAPPING_ATTRIBUTE = 'vudyDP7jUy5'

EXCHANGE_NAME_PREFIX = 'ADEX - HTN DM'
# A one-indicator patch exchange on sandbox 1, not part of the HTN/DM set.
EXCLUDE_IF_NAME_CONTAINS = ('Workaround',)

REQUEST_FIELDS = ('name,dx,pe,ou,filters,inputIdScheme,outputDataElementIdScheme,'
                  'outputOrgUnitIdScheme,outputIdScheme')


def _mapping_attribute(requests):
    """
    The attribute the requests map indicators through.

    Each request names it as outputIdScheme ATTRIBUTE:<uid>. They agree in
    practice, so the first one found wins and the default covers a config that
    states no scheme at all.
    """
    for request in requests:
        scheme = request.get('outputIdScheme') or ''
        if scheme.upper().startswith('ATTRIBUTE:'):
            return scheme.split(':', 1)[1]
    return DEFAULT_MAPPING_ATTRIBUTE


def _in_batches(items, size=30):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _target_elements(api, indicators, attribute):
    """Map each program indicator to the data element it writes into."""
    mapping = {}
    for batch in _in_batches(sorted(indicators)):
        found = api.get('programIndicators', params={
            'filter': 'id:in:[' + ','.join(batch) + ']',
            'fields': 'id,name,attributeValues[value,attribute[id]]',
            'paging': 'false'}).json()['programIndicators']
        for indicator in found:
            element = next((v['value'] for v in indicator.get('attributeValues', [])
                            if v['attribute']['id'] == attribute), None)
            mapping[indicator['id']] = (indicator['name'], element)
    return mapping


def _existing(api, element_uids):
    """Which of those data elements actually exist on this instance."""
    present = set()
    for batch in _in_batches(sorted(element_uids)):
        present.update(d['id'] for d in api.get('dataElements', params={
            'filter': 'id:in:[' + ','.join(batch) + ']',
            'fields': 'id', 'paging': 'false'}).json()['dataElements'])
    return present


def build(api, name_prefix=EXCHANGE_NAME_PREFIX):
    """
    Read this instance's exchanges and return (config, dropped).

    `dropped` maps a program indicator UID to its name for every indicator
    left out because the data element it names does not exist here. They are
    dropped rather than sent because DHIS2 rejects a whole posted batch over
    one unknown data element, and replace_adex.py stops at the first
    rejection - so a single missing element silently costs every value queued
    behind it. Sandbox 1 was missing two, which is how ten of its twelve DM
    indicators came to be empty.
    """
    exchanges = api.get('aggregateDataExchanges', params={
        'fields': f'id,name,target,source[requests[{REQUEST_FIELDS}]]',
        'paging': 'false'}).json()['aggregateDataExchanges']

    wanted = [e for e in exchanges
              if e['name'].startswith(name_prefix)
              and not any(skip in e['name'] for skip in EXCLUDE_IF_NAME_CONTAINS)]
    if not wanted:
        raise ValueError(f"no aggregate data exchange named '{name_prefix}...' "
                         f"on {api.base_url}")

    all_requests = [r for e in wanted for r in e['source']['requests']]
    attribute = _mapping_attribute(all_requests)
    indicators = {uid for r in all_requests for uid in r['dx']}

    mapping = _target_elements(api, indicators, attribute)
    present = _existing(api, {e for _, e in mapping.values() if e})
    dropped = {uid: name for uid, (name, element) in mapping.items()
               if element is None or element not in present}

    requests = []
    for exchange in wanted:
        # The exchange name is kept on each request so a run's output says
        # which period an empty result belongs to.
        suffix = exchange['name'].split(' - ')[-1]
        for request in exchange['source']['requests']:
            request = dict(request)
            request['dx'] = [uid for uid in request['dx'] if uid not in dropped]
            if not request['dx']:
                continue
            request['name'] = f"{request['name']} [{suffix}]"
            requests.append(request)

    config = {
        'name': f"{name_prefix} - built from {len(wanted)} exchange(s) on "
                f"{api.base_url}",
        'source': {'requests': requests},
        'target': wanted[0]['target'],
    }
    return config, dropped


def report(config, dropped):
    """Log what was built, and anything this instance cannot receive."""
    requests = config['source']['requests']
    logger.info(f"  {len(requests)} request(s), "
                f"{sum(len(r['dx']) for r in requests)} indicator(s)")
    for request in requests:
        logger.info(f"    {request['name']:<55} pe={request['pe']} "
                    f"dx={len(request['dx'])}")
    for uid, name in sorted(dropped.items(), key=lambda item: item[1]):
        logger.warning(f"  dropped {uid}: {name[:80]} - the data element it "
                       f"writes into does not exist on this instance")
