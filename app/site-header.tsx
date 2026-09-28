export default function SiteHeader({ active }: { active: "marketplace" | "analyze" }) {
  return <header className="topbar">
    <a className="brand" href="/" aria-label="DataScout marketplace"><span className="brand-mark">D<span>·</span></span><span>DataScout</span></a>
    <nav className="topnav" aria-label="Main navigation">
      <a href="/" aria-current={active === "marketplace" ? "page" : undefined}>Marketplace</a>
      <a href="/analyze" aria-current={active === "analyze" ? "page" : undefined}>Ask DataScout</a>
      <a href="/benchmark-review">Review benchmark →</a>
    </nav>
  </header>;
}
