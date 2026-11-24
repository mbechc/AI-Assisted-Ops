import { api, fmtDate, TERMS_URL, TermsRecord } from "../api";
import { useAsync } from "../components";
import UpdatesSection from "../UpdatesSection";

export default function Updates() {
  const { data: terms } = useAsync(() => api.get<TermsRecord>("/terms"), []);
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Updates</h1>
          <p className="sub">Installed version, update track and the newest release. Updates are installed by the host updater.</p>
        </div>
      </div>
      <UpdatesSection />
      <div className="card">
        <h3>About</h3>
        <p className="small">
          Steward is an independent open-source project, not a Cisco product, and is provided as is without warranty or
          support (<a href={TERMS_URL} target="_blank" rel="noreferrer">terms</a>).
        </p>
        <p className="small muted">
          {terms?.accepted
            ? <>Terms version {terms.terms_version} accepted by {terms.accepted_by} on {fmtDate(terms.accepted_at)} when installing {terms.steward_version} ({terms.method === "flag" ? "--accept-terms" : "typed on the terminal"}).</>
            : terms ? <>Terms not accepted on this installation yet: run <code>sudo /opt/Steward/scripts/accept-terms.sh</code> on the host.</> : null}
        </p>
      </div>
    </>
  );
}
