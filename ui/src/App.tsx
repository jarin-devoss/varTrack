import { Link, Route, Routes, useLocation } from 'react-router-dom';
import { EmptyState } from './components/common';
import { SectionCountProvider, usePageTitle } from './layout/page';
import { Shell } from './layout/Shell';
import { DashboardPage } from './pages/DashboardPage';
import { EnvironmentsPage } from './pages/EnvironmentsPage';
import { EventsPage } from './pages/EventsPage';
import { FavoritesPage } from './pages/FavoritesPage';
import { LoginPage } from './pages/LoginPage';
import { PlaygroundPage } from './pages/PlaygroundPage';
import { ProfilePage } from './pages/ProfilePage';
import { RulePage } from './pages/RulePage';
import { RulesPage } from './pages/RulesPage';
import { SettingsPage } from './pages/SettingsPage';
import { TaskPage } from './pages/TaskPage';
import { TasksPage } from './pages/TasksPage';

export function App() {
  return (
    <Routes>
      {/* The sign-in page stands alone, without the header and tabs. */}
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="*"
        element={
          <SectionCountProvider>
            <Shell>
              <Routes>
                <Route path="/" element={<DashboardPage />} />
                <Route path="/favorites" element={<FavoritesPage />} />
                <Route path="/rules" element={<RulesPage />} />
                <Route path="/rules/:platform/:datasource" element={<RulePage />} />
                <Route path="/environments" element={<EnvironmentsPage />} />
                <Route path="/events" element={<EventsPage />} />
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/tasks/:taskId" element={<TaskPage />} />
                <Route path="/playground" element={<PlaygroundPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="/settings/:section" element={<SettingsPage />} />
                <Route path="/profile" element={<ProfilePage />} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </Shell>
          </SectionCountProvider>
        }
      />
    </Routes>
  );
}

function NotFound() {
  const { pathname } = useLocation();
  usePageTitle('Not found');
  return (
    <div className="card">
      <EmptyState icon="helpCircle" title="Page not found" action={<Link className="btn btn--primary btn--sm" to="/">Go to the dashboard</Link>}>
        There is nothing at <code>{pathname}</code>.
      </EmptyState>
    </div>
  );
}
