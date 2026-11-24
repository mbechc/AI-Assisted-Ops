import AiSection from "../AiSection";

export default function AiAssistance() {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>AI assistance</h1>
          <p className="sub">
            Optional for every Expert Task: all tasks work on rules alone, and AI never executes a change. With AI on,
            AI-capable tasks get a second opinion on proposals, learn naming conventions, recognise site renames and write
            summaries. See <a href="https://github.com/mbechc/Steward/blob/master/docs/AI_USAGE.md" target="_blank" rel="noreferrer">AI usage</a>.
          </p>
        </div>
      </div>
      <AiSection />
    </>
  );
}
