import MatchesScreen from './screens/MatchesScreen'
import RematchStoredPostings from './screens/RematchStoredPostings'
import ResumeUploadScreen from './screens/ResumeUploadScreen'
import RunDigestNow from './screens/RunDigestNow'
import TelemetryScreen from './screens/TelemetryScreen'
import WatchlistScreen from './screens/WatchlistScreen'

/** The six screens, each named by its own heading and reached by its own id (#114 AC2). */
const SCREENS = [
  { id: 'resume', label: 'Resume' },
  { id: 'watchlist', label: 'Watched companies' },
  { id: 'matches', label: 'Matches' },
  { id: 'digest', label: 'Digest' },
  { id: 'rematch', label: 'Re-match' },
  { id: 'telemetry', label: 'Telemetry' },
]

/**
 * The one page: the resume upload, the watchlist, the matches, the two run controls
 * and the telemetry the two reads answer with.
 *
 * The nav is plain anchors at the screens' own ids, so a fragment shows the screen
 * it names and the app still needs no router (#114 AC2). The page with no fragment
 * keeps showing all six.
 */
export default function App() {
  return (
    <>
      <header className="site-header">
        <h1>JobRadar</h1>
        <nav aria-label="Screens">
          {SCREENS.map((screen) => (
            <a key={screen.id} href={`#${screen.id}`}>
              {screen.label}
            </a>
          ))}
        </nav>
      </header>
      <main>
        <ResumeUploadScreen />
        <WatchlistScreen />
        <MatchesScreen />
        <RunDigestNow />
        <RematchStoredPostings />
        <TelemetryScreen />
      </main>
    </>
  )
}
