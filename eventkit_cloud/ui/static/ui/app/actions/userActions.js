import actions from './actionTypes'
import {push} from 'react-router-redux'
import axios from 'axios'
import cookie from 'react-cookie'
import types from './actionTypes';


export const logout = query => dispatch => {

    return axios('/logout', {method: 'GET'}).then((response) => {
        dispatch({
            type: actions.USER_LOGGED_OUT,
        })
        dispatch(push({ pathname: '/login', search: query }));
    }).catch((error) => {
        console.log(error);
    });
}


export const login = (data, query) => (dispatch) => {

    const csrftoken = cookie.load('csrftoken');

    dispatch({
        type: actions.USER_LOGGING_IN,
    });

    const form_data = new FormData();
    var method = 'get';
    if (data && (data.username && data.password)) {
        form_data.append('username', data.username);
        form_data.append('password', data.password);
        method = 'post';
    }

    return axios({
        url: '/auth',
        method: method,
        data: form_data,
        headers: {"X-CSRFToken": csrftoken}
    }).then((response) => {
            if (response.data) {
                dispatch({
                    type: actions.USER_LOGGED_IN,
                    payload: response.data
                });
            } else {
                dispatch(logout(query));
            }
        }).catch((error) => {
        dispatch(logout(query));
    });
}

export const patchUser = (acceptedLicenses, username) => (dispatch) => {
    const csrftoken = cookie.load('csrftoken');
    dispatch({
        type: actions.PATCHING_USER,
    });

    return axios({
        url: '/api/user/' + username,
        method: 'PATCH',
        data: {accepted_licenses: acceptedLicenses},
        headers: {'X-CSRFToken': csrftoken}
    }).then((response) => {

        dispatch({
            type: actions.PATCHED_USER,
            payload: response.data || {"ERROR": "No user response data"}
        });
    }).catch((error) => {
        dispatch({
            type: actions.PATCHING_USER_ERROR,
            error: error
        });
    });
}

export const userActive = () => dispatch => {
    return axios('/auth', {method: 'GET'}).then(() => {
        dispatch({type: types.USER_ACTIVE, autoLogout: new Date(cookie.load('eventkit_auto_logout'))});
    }).catch((error) => {
        console.error(error.message);
    });
};
