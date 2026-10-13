import MatchesScreen from './screens/MatchesScreen'
import ResumeUploadScreen from './screens/ResumeUploadScreen'
import RunDigestNow from './screens/RunDigestNow'
import WatchlistScreen from './screens/WatchlistScreen'

/** The one page: the resume upload, the watchlist, the matches and the digest button. */
export default function App() {
  return (
    <main>
      <h1>JobRadar</h1>
      <ResumeUploadScreen />
      <WatchlistScreen />
      <MatchesScreen />
      <RunDigestNow />
    </main>
  )
}
