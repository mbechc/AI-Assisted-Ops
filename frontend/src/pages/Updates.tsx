import UpdatesSection from "../UpdatesSection";

export default function Updates() {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Updates</h1>
          <p className="sub">Installed version, update track and the newest release. Updates are installed by the host updater.</p>
        </div>
      </div>
      <UpdatesSection />
    </>
  );
}
