import React from 'react';
import { mount } from 'enzyme';
import sinon from 'sinon';
import AppBar from 'material-ui/AppBar';
import UserInfo from '../../components/AccountPage/UserInfo';
import Warning from '../../components/AccountPage/Warning';
import LicenseInfo from '../../components/AccountPage/LicenseInfo';
import SaveButton from '../../components/AccountPage/SaveButton';
import CustomScrollbar from '../../components/CustomScrollbar';
import { Account } from '../../components/AccountPage/Account';
import getMuiTheme from 'material-ui/styles/getMuiTheme';
import Joyride from 'react-joyride';
import Help from 'material-ui/svg-icons/action/help';
import {StatusDownload} from "../../components/StatusDownloadPage/StatusDownload";

describe('Account Component', () => {
    const muiTheme = getMuiTheme();

    const getProps = () => ({
        user: {
            data: {
                user: {
                    username: 'admin',
                    email: 'admin@admin.com',
                    date_joined: '2016-06-15T14:25:19Z',
                    last_login: '2016-06-15T14:25:19Z',
                },
                accepted_licenses: {
                    test1: false,
                    test2: false,
                },
            },
            isLoading: false,
            patched: false,
            patching: false,
            error: null,
        },
        licenses: {
            error: null,
            fetched: false,
            fetching: false,
            licenses: [
                { slug: 'test1', name: 'testname1', text: 'testtext1' },
                { slug: 'test2', name: 'testname2', text: 'textext2' },
            ],
        },
        getLicenses: () => {},
        patchUser: () => {},
    });

    const tooltipStyle = {
        backgroundColor: 'white',
        borderRadius: '0',
        color: 'black',
        mainColor: '#ff4456',
        textAlign: 'left',
        header: {
            textAlign: 'left',
            fontSize: '20px',
            borderColor: '#4598bf'
        },
        main: {
            paddingTop: '20px',
            paddingBottom: '20px',
        },

        button: {
            color: 'white',
            backgroundColor: '#4598bf'
        },
        skip: {
            color: '#8b9396'
        },
        back: {
            color: '#8b9396'
        },
        hole: {
            backgroundColor: 'rgba(226,226,226, 0.2)',
        }
    };
    const getMountedWrapper = props => mount(<Account {...props} />, {
        context: { muiTheme },
        childContextTypes: { muiTheme: React.PropTypes.object },
    });

    it('should call joyrideAddSteps when mounted', () => {
        const props = getProps();
        const joyrideSpy = new sinon.spy(Account.prototype, 'joyrideAddSteps');
        const mountSpy = new sinon.spy(Account.prototype, 'componentDidMount');
        const wrapper = getMountedWrapper(props);
        expect(mountSpy.calledOnce).toBe(true);
        expect(joyrideSpy.calledOnce).toBe(true);
        joyrideSpy.restore();
    });

    it('joyrideAddSteps should set state for steps in tour', () => {
        const steps = [{ title: 'Welcome to the Account Settings Page', text: 'This page contains Licenses and Terms of Use along with some personal information.  On your initial login, you must agree to these Licenses and Terms of Use to use EventKit.  You will only be required to re-visit this page in the future if new Licenses and Terms of Use are introduced with a new data provider.', selector: '.qa-Account-AppBar', position: 'top', style: tooltipStyle, isFixed: true, },
            { title: 'License Agreement Info', text: 'You can expand the license text and scroll down to review.  You can download the license text if you so choose.', selector: '.qa-UserLicense-ArrowDown', position: 'bottom', style: tooltipStyle, isFixed: true,},
            { title: 'Agree to Licenses', text: 'Once you’ve reviewed the licenses, you can agree to them individually.', selector: '.qa-UserLicense-Checkbox', position: 'bottom', style: tooltipStyle, isFixed: true,},
            { title: 'Agree to Licenses', text: 'Or you can choose to agree to them collectively.', selector: '.qa-LicenseInfo-Checkbox', position: 'bottom', style: tooltipStyle, isFixed: true,},
            { title: 'Save Agreements', text: 'Once you have selected the licenses to agree to, click Save Changes.', selector: '.qa-SaveButton-RaisedButton-SaveChanges', position: 'top', style: tooltipStyle, isFixed: true,},
            { title: 'Navigate Application', text: 'Once you have saved the license agreements, you can navigate away from the page to browse DataPacks.', selector: '.qa-Application-MenuItem-exports', position: 'top', style: tooltipStyle, isFixed: true,},
            { title: 'Navigate Application', text: 'Or to create your own DataPack.', selector: '.qa-Application-MenuItem-create', position: 'top', style: tooltipStyle, isFixed: true, }, ];
        const props = getProps();
        const wrapper = getMountedWrapper(props);
        const stateSpy = new sinon.spy(Account.prototype, 'setState');
        wrapper.instance().joyrideAddSteps(steps);
        expect(stateSpy.calledOnce).toBe(true);
        expect(stateSpy.calledWith({ steps: steps }));
        stateSpy.restore();
    });

    it('handleJoyride should set state', () => {
        const props = getProps();
        const wrapper = getMountedWrapper(props);
        const stateSpy = new sinon.spy(Account.prototype, 'setState');
        wrapper.instance().handleJoyride();
        expect(stateSpy.calledOnce).toBe(true);
        expect(stateSpy.calledWith({ isRunning: false }));
        stateSpy.restore();
    });

    it('callback function should stop tour if close is clicked', () => {
        const callbackData = {
            action: "close",
            index: 2,
            step: {
                position: "bottom",
                selector: ".qa-Application-MenuItem-create",
                style: tooltipStyle,
                text: "Or to create your own DataPack.",
                title: "Navigate Application",
            },
            type: "step:before",
        }
        const props = getProps();
        const wrapper = getMountedWrapper(props);
        const stateSpy = new sinon.spy(Account.prototype, 'setState');
        wrapper.instance().callback(callbackData);
        expect(stateSpy.calledOnce).toBe(true);
        expect(stateSpy.calledWith({isRunning: false}));
        stateSpy.restore();
    });

    it('should render an appbar with save button, and body with license info and user info', () => {
        const props = getProps();
        const wrapper = getMountedWrapper(props);
        expect(wrapper.find(AppBar)).toHaveLength(1);
        expect(wrapper.find(AppBar).text()).toEqual('AccountPage TourSave Changes');
        expect(wrapper.find(SaveButton)).toHaveLength(1);
        expect(wrapper.find(CustomScrollbar)).toHaveLength(1);
        expect(wrapper.find(LicenseInfo)).toHaveLength(1);
        expect(wrapper.find(UserInfo)).toHaveLength(1);
        expect(wrapper.find(Joyride)).toHaveLength(1);
        expect(wrapper.find(Help)).toHaveLength(1);
    });

    it('should not render license info or or user info', () => {
        const props = getProps();
        props.user.data.user = {};
        props.licenses.licenses = [];
        const wrapper = getMountedWrapper(props);
        expect(wrapper.find(LicenseInfo)).toHaveLength(0);
        expect(wrapper.find(UserInfo)).toHaveLength(0);
    });

    it('should setState and call getLicenses when mounting', () => {
        const props = getProps();
        props.getLicenses = new sinon.spy();
        const mountSpy = new sinon.spy(Account.prototype, 'componentWillMount');
        const stateSpy = new sinon.spy(Account.prototype, 'setState');
        const wrapper = getMountedWrapper(props);
        expect(mountSpy.calledOnce).toBe(true);
        expect(stateSpy.calledWith({ acceptedLicenses: props.user.data.accepted_licenses })).toBe(true);
        expect(props.getLicenses.calledOnce).toBe(true);
        mountSpy.restore();
        stateSpy.restore();
    });

    it('should update state and setTimeout when user has been patched', () => {
        jest.useFakeTimers();
        const props = getProps();
        const stateSpy = new sinon.spy(Account.prototype, 'setState');
        const wrapper = getMountedWrapper(props);
        expect(stateSpy.calledWith({ showSavedMessage: true })).toBe(false);
        const nextProps = getProps();
        nextProps.user.patched = true;
        wrapper.setProps(nextProps);
        expect(stateSpy.calledWith({ showSavedMessage: true })).toBe(true);
        expect(stateSpy.calledWith({ showSavedMessage: false })).toBe(false);
        jest.runAllTimers();
        expect(stateSpy.calledWith({ showSavedMessage: false })).toBe(true);
        expect(setTimeout.mock.calls.length).toBe(1);
        expect(setTimeout.mock.calls[0][1]).toBe(3000);
        stateSpy.restore();
    });

    it('handleCheck should marked the license as checked/unchecked and update state', () => {
        const props = getProps();
        const stateSpy = new sinon.spy(Account.prototype, 'setState');
        const wrapper = getMountedWrapper(props);
        expect(wrapper.state().acceptedLicenses).toEqual({ ...props.user.data.accepted_licenses });
        wrapper.instance().handleCheck('test1', true);
        expect(stateSpy.calledWith({ acceptedLicenses: { ...props.user.data.accepted_licenses, test1: true } }));
        stateSpy.restore();
    });

    it('handleAll should check all as checked/unchecked and update the state', () => {
        const props = getProps();
        const stateSpy = new sinon.spy(Account.prototype, 'setState');
        const wrapper = getMountedWrapper(props);
        expect(wrapper.state().acceptedLicenses).toEqual({ ...props.user.data.accepted_licenses });
        wrapper.instance().handleAll({}, true);
        expect(stateSpy.calledWith({ acceptedLicenses: { test1: true, test2: true } }));
        stateSpy.restore();
    });

    it('handleAll should not uncheck already agreed licenses', () => {
        const props = getProps();
        props.user.data.accepted_licenses.test1 = true;
        const stateSpy = new sinon.spy(Account.prototype, 'setState');
        const wrapper = getMountedWrapper(props);
        expect(wrapper.state().acceptedLicenses).toEqual({ test1: true, test2: false });
        wrapper.setState({ acceptedLicenses: { test1: true, test2: true } });
        expect(wrapper.state().acceptedLicenses).toEqual({ test1: true, test2: true });
        wrapper.instance().handleAll({}, false);
        expect(stateSpy.calledWith({ test1: true, test2: false }));
        stateSpy.restore();
    });

    it('handleSubmit should call patchUser', () => {
        const props = getProps();
        props.patchUser = new sinon.spy();
        const wrapper = getMountedWrapper(props);
        expect(props.patchUser.notCalled).toBe(true);
        wrapper.instance().handleSubmit();
        expect(props.patchUser.calledWith(wrapper.state().acceptedLicenses, props.user.data.user.username)).toBe(true);
    });
});
