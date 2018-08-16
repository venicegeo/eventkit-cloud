import React from 'react';
import pure from 'recompose/pure';
import SvgIcon from 'material-ui/SvgIcon';

const icon = props => (
    <SvgIcon {...props}>
        <path
            d="m 4.2922912,7.053913 c 0.9357595,0 1.7237685,-0.7880072 1.7237685,-1.723767 0,-0.93576 -0.788009,
            -1.7237686 -1.7237685,-1.7237686 -0.9357601,0 -1.7237687,0.7880086 -1.7237687,
            1.7237686 0,0.9357598 0.7387584,1.723767 1.7237687,1.723767 z"
        />
        <path
            d="M 4.2922912,7.743418 C 3.0117773,7.743418 0.5,8.383682 0.5,9.664192 V 11.24021 H 8.0845853 V 9.664192 c 0,
            -1.28051 -2.5610336,-1.920774 -3.7922941,-1.920774 z"
        />
        <path
            d="m 19.70771,7.053913 c 0.935757,0 1.723766,-0.7880072 1.723766,-1.723767 0,-0.93576 -0.788009,-1.7237686 -1.723766,
            -1.7237686 -0.935765,0 -1.723772,0.7880086 -1.723772,1.7237686 0,0.9357598 0.738756,1.723767 1.723772,1.723767 z"
        />
        <path
            d="m 19.70771,7.743418 c -1.280519,0 -3.79229,0.640264 -3.79229,1.920774 V 11.24021 H 23.5 V 9.664192 c 0,
            -1.28051 -2.561032,-1.920774 -3.79229,-1.920774 z"
        />
        <path
            d="m 12.172379,11.48646 c 1.231264,0 2.216268,-0.985006 2.216268,-2.216274 0,-1.231256 -0.985004,-2.216273 -2.216268,
            -2.216273 -1.231264,0 -2.216267,0.985017 -2.216267,2.216273 0,1.231268 0.985003,2.216274 2.216267,2.216274 z"
        />
        <path
            d="m 12.172379,12.619224 c -1.625272,0 -4.9250544,0.837261 -4.9250544,2.462525 v 2.019275 h 9.8008584 v -2.019275 c 0,
            -1.674513 -3.25054,-2.462525 -4.875804,-2.462525 z"
        />
        <path
            d="m 11.826172,16.755859 v 3.398438 h 0.691406 v -3.398438 z"
        />
        <path
            d="m 3.8971326,12.619225 v 7.881047 H 20.053617 v -7.881047 h -0.691624 v 7.189424 H 4.5887563 v -7.189424 z"
        />
    </SvgIcon>
);

const AdminShareIcon = pure(icon);
AdminShareIcon.displayName = 'AdminShareIcon';
AdminShareIcon.muiName = 'SvgIcon';

export default AdminShareIcon;
