import { lazy, Suspense } from 'react';
import { createBrowserRouter, Outlet, RouterProvider } from 'react-router-dom';
import { Sidebar } from './components/Sidebar';
import { Footer } from './components/Footer';
import { SkeletonCard, SkeletonRows } from './components/Skeleton';

// Route-level code splitting: AG Grid (Screener) and Plotly (Dashboard's
// sparkline, Ticker Detail's chart) are the two biggest dependencies in the
// bundle, and only one of the three screens needs AG Grid at all -- lazy
// imports keep them out of the entry chunk instead of paying for all three
// screens' dependencies on first paint.
const Configuration = lazy(() => import('./screens/Configuration').then((m) => ({ default: m.Configuration })));
const Diagnostics = lazy(() => import('./screens/Diagnostics').then((m) => ({ default: m.Diagnostics })));
const Dashboard = lazy(() => import('./screens/Dashboard').then((m) => ({ default: m.Dashboard })));
const Screener = lazy(() => import('./screens/Screener').then((m) => ({ default: m.Screener })));
const TickerDetailScreen = lazy(() =>
  import('./screens/TickerDetail').then((m) => ({ default: m.TickerDetailScreen })),
);

function RouteSkeleton() {
  return (
    <div className="flex flex-col gap-4 p-6">
      <SkeletonRows rows={1} className="h-8 w-1/3" />
      <SkeletonCard lines={3} />
      <SkeletonCard lines={4} />
    </div>
  );
}

function Layout() {
  return (
    <div className="flex h-full min-h-screen flex-col">
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-y-auto">
          <Suspense fallback={<RouteSkeleton />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
      <Footer />
    </div>
  );
}

const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: 'screener', element: <Screener /> },
      { path: 'configuration', element: <Configuration /> },
      { path: 'diagnostics', element: <Diagnostics /> },
      { path: 'tickers/:symbol', element: <TickerDetailScreen /> },
    ],
  },
]);

function App() {
  return <RouterProvider router={router} />;
}

export default App;
