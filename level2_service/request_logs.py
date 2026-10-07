from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Callable, Mapping
@dataclass(frozen=True)
class RequestLogEntry:
 timestamp: datetime; ip: str|None = None; user_id: int|None = None; user_name: str|None = None; device_type: str|None = None; user_agent: str|None = None; path: str = ""; action: str = "unknown"; symbol: str|None = None; stock_name: str|None = None; status_code:int = 200; task_status:str|None = None; error_code:str|None = None; error_detail:str|None = None; duration_ms:float = 0.0; public_id:str|None = None
@dataclass(frozen=True)
class RequestLog:
 id:int; timestamp:str; ip:str|None; user_id:int|None; user_name:str|None; device_type:str|None; user_agent:str|None; path:str; action:str; symbol:str|None; stock_name:str|None; status_code:int; task_status:str|None; error_code:str|None; error_detail:str|None; duration_ms:float; public_id:str|None
class RequestLogStore:
 def __init__(self,path:Path):
  self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.connection=sqlite3.connect(self.path,check_same_thread=False); self.connection.row_factory=sqlite3.Row
  self.connection.execute("CREATE TABLE IF NOT EXISTS request_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT NOT NULL,ip TEXT,user_id INTEGER,user_name TEXT,device_type TEXT,user_agent TEXT,path TEXT NOT NULL,action TEXT NOT NULL DEFAULT 'unknown',symbol TEXT,stock_name TEXT,status_code INTEGER NOT NULL,task_status TEXT,error_code TEXT,error_detail TEXT,duration_ms REAL NOT NULL,public_id TEXT)")
  for sql in ("ALTER TABLE request_logs ADD COLUMN user_id INTEGER","ALTER TABLE request_logs ADD COLUMN user_name TEXT","ALTER TABLE request_logs ADD COLUMN action TEXT NOT NULL DEFAULT 'unknown'","ALTER TABLE request_logs ADD COLUMN error_detail TEXT"):
   try:self.connection.execute(sql)
   except sqlite3.OperationalError:pass
  self.connection.commit()
 def record(self,e:RequestLogEntry):
  self.connection.execute("INSERT INTO request_logs(timestamp,ip,user_id,user_name,device_type,user_agent,path,action,symbol,stock_name,status_code,task_status,error_code,error_detail,duration_ms,public_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(e.timestamp.astimezone(timezone.utc).isoformat(),e.ip,e.user_id,e.user_name,e.device_type,e.user_agent[:512] if e.user_agent else None,e.path,e.action,e.symbol,e.stock_name,e.status_code,e.task_status,e.error_code,e.error_detail[:256] if e.error_detail else None,e.duration_ms,e.public_id)); self.connection.commit()
 def backfill_stock_names(self, resolve_name: Callable[[str], str | None]) -> int:
  rows = self.connection.execute(
   "SELECT DISTINCT symbol FROM request_logs "
   "WHERE stock_name IS NULL AND symbol IS NOT NULL"
  ).fetchall()
  updated = 0
  with self.connection:
   for row in rows:
    symbol = str(row["symbol"])
    try:
     name = resolve_name(symbol)
    except Exception:
     continue
    if not isinstance(name, str) or not name.strip():
     continue
    result = self.connection.execute(
     "UPDATE request_logs SET stock_name=? "
     "WHERE symbol=? AND stock_name IS NULL",
     (name.strip(), symbol),
    )
    updated += result.rowcount
  return updated
 def list(self,limit:int,offset:int,filters:Mapping[str,str|None]|None=None):
  f=filters or {}; w=[]; a=[]
  if f.get('status') == '成功': w.append("status_code < 400 AND (task_status IS NULL OR task_status NOT IN ('FAILED','PARTIAL'))")
  elif f.get('status') == '失败': w.append("(status_code >= 400 OR task_status IN ('FAILED','PARTIAL'))")
  for k,c in [('action','action'),('user_id','user_id')]:
   if f.get(k):w.append(c+' = ?');a.append(f[k])
  for k,c in [('symbol','symbol'),('stock_name','stock_name'),('user_name','user_name'),('ip','ip'),('device_type','device_type'),('user_agent','user_agent'),('path','path'),('error_code','error_code'),('public_id','public_id')]:
   if f.get(k):w.append(c+' LIKE ?');a.append('%'+f[k]+'%')
  for k,op in [('from','>='),('to','<=')]:
   if f.get(k): w.append('timestamp '+op+' ?'); a.append(f[k])
  q=(' WHERE '+' AND '.join(w)) if w else ''; total=self.connection.execute('SELECT COUNT(*) FROM request_logs'+q,a).fetchone()[0]; rows=self.connection.execute('SELECT * FROM request_logs'+q+' ORDER BY timestamp DESC LIMIT ? OFFSET ?',a+[limit,offset]).fetchall(); return [RequestLog(**dict(r)) for r in rows],total
 def purge_before(self,cutoff:datetime)->int:
  c=self.connection.execute('DELETE FROM request_logs WHERE timestamp < ?',(cutoff.astimezone(timezone.utc).isoformat(),));self.connection.commit();return c.rowcount
 def close(self):self.connection.close()
