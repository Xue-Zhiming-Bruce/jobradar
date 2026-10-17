import MatchesScreen from './screens/MatchesScreen'
import RematchStoredPostings from './screens/RematchStoredPostings'
import ResumeUploadScreen from './screens/ResumeUploadScreen'
import RunDigestNow from './screens/RunDigestNow'
import TelemetryScreen from './screens/TelemetryScreen'
import WatchlistScreen from './screens/WatchlistScreen'

/**
 * The one page: the resume upload, the watchlist, the matches, the two run controls
 * and the telemetry the two reads answer with.
 */
export default function App() {
  return (
    <main>
      <h1>JobRadar</h1>
      <ResumeUploadScreen />
      <WatchlistScreen />
      <MatchesScreen />
      <RunDigestNow />
      <RematchStoredPostings />
      <TelemetryScreen />
    </main>
  )
}
