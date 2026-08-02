/**
 * AG Grid's Theming API (v33+), retuned to this app's own design tokens
 * rather than a stock AG Grid palette -- "AG Grid dark theme customised to
 * match" per the design brief. No AG Grid CSS files are imported anywhere;
 * the Theming API generates scoped styles at runtime from this object.
 */
import { themeQuartz, colorSchemeDark } from 'ag-grid-community';

const tokens = {
  page: '#0d0d0d',
  surface: '#1a1a19',
  surface2: '#202020',
  gridline: '#2c2c2a',
  ink: '#ffffff',
  inkSecondary: '#c3c2b7',
  inkMuted: '#898781',
  accent: '#3987e5',
  accentSoft: 'rgba(57, 135, 229, 0.16)',
};

export const claudeTradeGridTheme = themeQuartz.withPart(colorSchemeDark).withParams({
  backgroundColor: tokens.surface,
  foregroundColor: tokens.ink,
  headerBackgroundColor: tokens.surface2,
  headerTextColor: tokens.inkSecondary,
  headerFontWeight: 600,
  oddRowBackgroundColor: 'color-mix(in srgb, ' + tokens.surface + ' 96%, white 4%)',
  borderColor: tokens.gridline,
  rowBorder: true,
  rowHoverColor: tokens.accentSoft,
  selectedRowBackgroundColor: 'rgba(57, 135, 229, 0.22)',
  rangeSelectionBorderColor: tokens.accent,
  inputFocusBorder: { color: tokens.accent },
  fontFamily: [
    '-apple-system',
    'BlinkMacSystemFont',
    '"Segoe UI"',
    'Roboto',
    'Helvetica',
    'Arial',
    'sans-serif',
  ],
  fontSize: 13,
  headerHeight: 38,
  rowHeight: 40,
  cellHorizontalPadding: 14,
  wrapperBorder: false,
  browserColorScheme: 'dark',
});
