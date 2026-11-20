"""Capture a local editor preview cache inventory.

v1 manifests keep the historical shape (id/size_mb/last_used) and remain
record-only: without a real path or digest they can never be executed against.

v2 manifests additionally carry the physical evidence the executable pruner
needs: cache root, file path, SHA-256 digest, physical identity (so same-content
copies share one byte count), generation dependencies, generation cost, transient
workspace demand and durable engineering archives that must never be removed.
Active review references arrive under ``references`` and are persisted as facts
independent of any later prune request.
"""
from .ingest import capture
from .store import (connect, save, decide, save_physical, save_dependency,
                    save_reference, issue)

LEGACY_STATUS = '已缓存'
ARCHIVE_STATUS = '已归档'


def _physical_identity(row):
    if row.get('physical_id'):
        return str(row['physical_id'])
    digest = row.get('sha256')
    if digest:
        return f'phys:{digest[:16]}'
    # Legacy rows expose no digest: every record is its own opaque identity and
    # the executor must refuse to touch it.
    return f'legacy:{row["id"]}'


def import_cache(path, file):
    source, payload = capture(path, 'desktop_preview_cache', file)
    db = connect(path)
    results = []
    try:
        with db:
            cache_root = payload.get('cache_root')
            for row in payload.get('rows', []):
                key = str(row['id'])
                stored = dict(row)
                if cache_root and 'cache_root' not in stored:
                    stored['cache_root'] = cache_root
                physical_id = _physical_identity(stored)
                stored['physical_id'] = physical_id
                size_mb = float(stored.get('size_mb', 0))
                if stored.get('durable'):
                    status, reason = ARCHIVE_STATUS, 'durable_archive_protected'
                else:
                    status, reason = LEGACY_STATUS, 'inventory_captured'
                save(db, 'preview_cache', key, source, payload['version'],
                     stored, status, reason)
                save_physical(db, physical_id, source, stored.get('sha256', ''),
                              size_mb, {'id': key, 'physical_id': physical_id,
                                        'path': stored.get('path'),
                                        'size_mb': size_mb,
                                        'durable': bool(stored.get('durable'))})
                for parent in stored.get('depends_on', []) or []:
                    save_dependency(db, key, str(parent), source)
                results.append(decide(db, '预览缓存', key, status, reason, source))
            for ref in payload.get('references', []) or []:
                target = str(ref['target_id'])
                ref_id = str(ref.get('id') or f'ref:{target}:{ref.get("session", "default")}')
                save_reference(db, ref_id, source, target, ref.get('session'),
                               ref.get('active_from'), ref.get('active_until'), ref)
                results.append(decide(db, '活动审校引用', ref_id, '已采录',
                                      'active_reference_captured', source))
            for outside in payload.get('outside_cache', []) or []:
                # Named durable archives outside the cache root: record the fact
                # so the executor can prove it never treats them as targets.
                issue(db, source, 'outside_cache_protected', str(outside))
            return results
    finally:
        db.close()
