from __future__ import annotations
import json
from pathlib import Path

def load_contemplas_manifest(path):
    path=Path(path).resolve(); payload=json.loads(path.read_text(encoding='utf-8')); m=payload.get('manifest',payload)
    if not isinstance(m,dict) or 'cameras' not in m: raise ValueError('Not a CONTEMPLAS manifest')
    sid=str(m.get('sessionId') or m.get('source',{}).get('recordingId') or path.parent.name); fps=float(m.get('timing',{}).get('fps') or 100)
    cams=[]
    for i,c in enumerate(m.get('cameras') or []):
        src=c.get('source') or {}; segs=src.get('containerSegments') or []
        if str(src.get('format','')).lower()!='bcap' or not segs:continue
        raw=Path(str(segs[0].get('path') or '')); candidate=raw if raw.exists() else path.parent/raw.name
        media=c.get('media') or {}; pres=c.get('presentation') or {}
        cams.append({'camera_id':str(c.get('cameraId') or f'camera-{i}'),'label':str(c.get('label') or f'Camera {i+1}'),'path':candidate.resolve(),'fps':float(media.get('fps') or fps),'frame_count':int(media.get('frameCount') or 0),'rotation':int(round(float(pres.get('quarterTurnDegrees') or 0)))%360,'sync_offset_frames':int(c.get('syncOffsetFrames') or 0),'sync_group_id':f'bcap:{sid}'})
    started=m.get('source',{}).get('startedAt') or ((m.get('clips') or [{}])[0].get('startedAt'))
    return {'session_id':sid,'sync_group_id':f'bcap:{sid}','fps':fps,'started_at':started,'cameras':cams,'manifest_path':path}
