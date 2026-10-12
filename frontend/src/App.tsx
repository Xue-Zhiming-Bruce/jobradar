import MatchesScreen from './screens/MatchesScreen'
import RunDigestNow from './screens/RunDigestNow'
import WatchlistScreen from './screens/WatchlistScreen'

/** The one page: the watchlist, the matches and the button that starts a digest. */
export default function App() {
  return (
    <main>
      <h1>JobRadar</h1>
      <WatchlistScreen />
      <MatchesScreen />
      <RunDigestNow />
    </main>
  )
}
