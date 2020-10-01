import * as React from 'react';
import {CreateDataPackButton} from "../../components/StatusDownloadPage/CreateDataPackButton";
import {render, screen, getByText, waitFor, fireEvent} from '@testing-library/react';
import {useRunContext} from "../../components/StatusDownloadPage/RunFileContext";
import '@testing-library/jest-dom/extend-expect'
import MockAdapter from "axios-mock-adapter";
import axios from "axios";

jest.mock('../../components/StatusDownloadPage/RunFileContext', () => {
    return {
        useRunContext: jest.fn(),
    }
});

jest.mock('../../components/Dialog/BaseDialog', () => 'dialog');
jest.mock('../../components/common/CenteredPopup', () => 'centeredPopup');


describe('CreateDataPackButton component', () => {
    const defaultProps = () => ({
        fontSize: '12px',
        providerTaskUids: ['thisistotallyauid'],
        classes: {},
        theme: {eventkit: {
                images: {},
                colors: {}
            }},
        ...(global as any).eventkit_test_props,
    });

    const setup = (propsOverride = {}) => {
        (useRunContext as any).mockImplementation(() => {
            return {run: {status: 'COMPLETED'}}
        })
        const props = {
            ...defaultProps(),
            ...propsOverride,
        };
        return render(<CreateDataPackButton {...props} />);
    };

    it('should say job processing when job is not complete.', () => {
        const {container, rerender} = setup();
        (useRunContext as any).mockImplementation(() => {
            return {run: {status: 'a not correct value'}}
        })
        rerender(<CreateDataPackButton {...defaultProps()}/>)
        expect(getByText(container,/Job Processing.../)).toBeInTheDocument();
    });

    it('should display create text by default when job is done.', () => {
        setup();
        expect(screen.getByText(/CREATE DATAPACK/)).toBeInTheDocument();
    });

    it('should disable button after click and render fake button.', async () => {
        const {container} = setup();
        expect(container.querySelector('#qa-CreateDataPackButton-fakeButton')).not.toBeInTheDocument();
        const mock = new MockAdapter(axios, {delayResponse: 10});
        mock.onGet(`/api/runs/zipfiles`).reply(200, []);
        fireEvent(
            screen.getByText(/CREATE DATAPACK/),
            new MouseEvent('click', {
                bubbles: true,
                cancelable: true,
            })
        );
        expect(container.querySelector(
            '#qa-CreateDataPackButton-fakeButton')
        ).toBeInTheDocument()
    });
});
