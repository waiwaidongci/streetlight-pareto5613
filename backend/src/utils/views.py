from src.utils.time import iso


def dataset(item):
    return {'id': item.id, 'name': item.name, 'filename': item.filename, 'row_count': item.row_count,
            'checksum': item.checksum, 'author': item.author, 'created_at': iso(item.created_at)}


def run(item, detail=False):
    item_view = {'id': item.id, 'dataset_id': item.dataset_id, 'name': item.name, 'status': item.status,
                 'author': item.author, 'review': item.review, 'decision': item.decision,
                 'created_at': iso(item.created_at), 'finished_at': iso(item.finished_at),
                 'scheduled_at': iso(item.scheduled_at), 'schedule_id': item.schedule_id,
                 'has_export': bool(item.export_file), 'error': item.error}
    if detail:
        item_view.update(params=item.params, result=item.result, review_note=item.review_note)
    return item_view


def schedule(item):
    from src.domain.spec import SPEC
    return {'id': item.id, 'name': item.name, 'dataset_id': item.dataset_id, 'recipient': item.recipient,
            'next_at': iso(item.next_at), 'repeat_daily': item.repeat_daily, 'enabled': item.enabled,
            'next_report_day': item.params.get(SPEC['schedule_date_param']) if item.enabled else None}


def delivery(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'state': item.state, 'detail': item.detail, 'created_at': iso(item.created_at)}


def judgment(item):
    return {'id': item.id, 'dataset_id': item.dataset_id, 'left_ref': item.left_ref,
            'right_ref': item.right_ref, 'relation': item.relation, 'note': item.note,
            'author': item.author, 'created_at': iso(item.created_at)}


def consolidation_version(item, detail=False):
    view = {'id': item.id, 'dataset_id': item.dataset_id, 'version_no': item.version_no,
            'is_active': item.is_active, 'status': item.status, 'author': item.author,
            'created_at': iso(item.created_at), 'activated_at': iso(item.activated_at),
            'statistics': item.payload.get('statistics') if item.payload else None,
            'contradictions': item.contradictions}
    if detail:
        view['payload'] = item.payload
        view['judgment_fingerprint'] = item.judgment_fingerprint
        view['input_fingerprint'] = item.input_fingerprint
    return view
