import { useEffect, useState } from "react";
import RunPage from "./RunPage";
import RunsPage from "./RunsPage";
import { STATIC } from "./api";

// Two routes, so a few lines of history handling instead of a router dependency:
//   /               -> runs list
//   /runs/<run-id>  -> one run
// The static build (GitHub Pages) routes by hash (#/runs/<id>): a static host can't serve deep paths.
function currentPath() {
  return STATIC ? window.location.hash.slice(1) || "/" : window.location.pathname;
}

export function navigate(path: string) {
  if (STATIC) {
    window.location.hash = path; // fires hashchange
  } else {
    window.history.pushState(null, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }
  window.scrollTo(0, 0);
}

export default function App() {
  const [path, setPath] = useState(currentPath);

  useEffect(() => {
    const onChange = () => setPath(currentPath());
    const event = STATIC ? "hashchange" : "popstate";
    window.addEventListener(event, onChange);
    return () => window.removeEventListener(event, onChange);
  }, []);

  const runMatch = path.match(/^\/runs\/([^/]+)$/);
  return (
    <div className="shell">
      <aside className="sidebar">
        <a href="/" onClick={(e) => { e.preventDefault(); navigate("/"); }} className="brand">
          <span className="brand-mark" aria-hidden>⇡</span>
          <span>
            Molt
            <span className="brand-sub">run viewer</span>
          </span>
        </a>
        <nav className="side-nav">
          <a href="/" onClick={(e) => { e.preventDefault(); navigate("/"); }} className={runMatch ? "" : "active"}>
            Runs
          </a>
          {STATIC && <a href="../">About the project</a>}
        </nav>
        <div className="sidebar-foot muted small">
          {STATIC ? "Read-only snapshot of the published benchmark runs" : <>Reads <span className="mono">runs/</span> · updates live</>}
        </div>
      </aside>
      <main className="content">
        {runMatch ? <RunPage runId={decodeURIComponent(runMatch[1])} /> : <RunsPage />}
      </main>
    </div>
  );
}
