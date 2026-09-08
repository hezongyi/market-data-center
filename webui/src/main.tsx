import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';

function App() {
  const [health, setHealth] = useState('loading');
  const [runs, setRuns] = useState<any[]>([]);
  useEffect(() => {
    Promise.all([fetch('/api/v1/health').then(r => r.json()), fetch('/api/v1/runs').then(r => r.json())])
      .then(([h, r]) => { setHealth(h.data.status); setRuns(r.data); });
  }, []);
  return <main><header><h1>Market Data Center</h1><span className="status">{health}</span></header>
    <section><h2>Recent ingest runs</h2>{runs.length === 0 ? <p>No runs yet.</p> : <table><tbody>{runs.map(run => <tr key={run.run_id}><td>{run.run_id}</td><td>{run.dataset_id}</td><td>{run.status}</td><td>{run.row_count}</td></tr>)}</tbody></table>}</section>
  </main>;
}
createRoot(document.getElementById('root')!).render(<App />);

