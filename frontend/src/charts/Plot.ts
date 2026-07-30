// The full `plotly.js` package is several MB; `plotly.js-dist-min` is the
// same public API pre-built and minified, wired in via react-plotly.js's
// factory entry point so we never pull in both.
import createPlotlyComponent from 'react-plotly.js/factory';
import Plotly from 'plotly.js-dist-min';

const Plot = createPlotlyComponent(Plotly);
export default Plot;
