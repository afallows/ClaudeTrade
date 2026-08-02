import { ModuleRegistry, AllCommunityModule } from 'ag-grid-community';

// Registered once, at module load. Community-only -- no enterprise modules
// are registered or imported anywhere in this app.
ModuleRegistry.registerModules([AllCommunityModule]);
